"""The M-Pesa number entered when paying: body handling, resolution, replays, limits, locking, responses.

No database or network: the request and payment sessions are scripted fakes and Daraja is faked or
served by an ``httpx.MockTransport``.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from payment_fakes import (
    BUYER_PHONE,
    NOW,
    TEST_MSISDN,
    FakeDaraja,
    FakeDB,
    FakeResult,
    bound_values,
    make_buyer,
    make_order,
    make_payment,
    payment_settings,
    use_settings,
    view_row,
)
from response_helpers import without_request_id

from app.core.dependencies import get_current_user
from app.db import get_db, get_session_factory
from app.integrations.daraja import STK_PUSH_PATH, DarajaClient, get_daraja_client
from app.models.enums import OrderStatus, PaymentStatus
from app.services import payment_service

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PAY_KEY = "pay-key-0001"
PAY_HEADERS = {"Idempotency-Key": PAY_KEY}
PROFILE_PHONE = "254799000111"
ENTERED = "254712345678"
INVALID_PHONE = {
    "error": {"code": "invalid_phone", "message": "Enter a valid Safaricom number, for example 0712 345 678."}
}


@pytest.fixture
def settings(monkeypatch):
    return use_settings(monkeypatch, payment_settings())


@pytest.fixture(autouse=True)
def no_rematch(monkeypatch):
    async def fake_rematch(session_factory, checkout_request_id):
        del session_factory, checkout_request_id

    monkeypatch.setattr(payment_service, "rematch_orphan_events", fake_rematch)


def install(application, db: FakeDB, user, client) -> None:
    application.dependency_overrides[get_current_user] = lambda: user

    async def request_session():
        yield db.session()

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_session_factory] = lambda: db.factory
    application.dependency_overrides[get_daraja_client] = lambda: client


def pay_db(buyer, order=None, *, db_now=NOW, recent=0, buyer_recent=0, used_phones=(), view=None):
    """Script a full pay request (see ``test_payments.pay_db`` for the statement order)."""
    order = order or make_order(buyer.id)
    db = FakeDB(
        scalars=[None, order, db_now, 0, recent, buyer_recent, order, lambda db: db.payments[-1]],
        lists=[[]],
        results=[FakeResult(rows=list(used_phones)), FakeResult(mapping_rows=[view or view_row(order)])],
    )
    return db, order


async def post_pay(api_client, order_id, headers=PAY_HEADERS, **kwargs):
    return await api_client.post(f"/api/v1/orders/{order_id}/pay", headers=headers, **kwargs)


def statements_with(db: FakeDB, fragment: str) -> list[int]:
    return [index for index, sql in enumerate(db.statements) if fragment in sql]


# Body handling -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, PROFILE_PHONE),
        ({"json": {}}, PROFILE_PHONE),
        ({"json": {"phone_number": None}}, PROFILE_PHONE),
        ({"json": {"phone_number": "0712 345 678"}}, ENTERED),
        (
            {
                "content": b'{"phone_number": "+254712345678"}',
                "headers": {**PAY_HEADERS, "Content-Type": "application/json"},
            },
            ENTERED,
        ),
    ],
)
async def test_no_body_an_empty_object_and_a_number_all_reach_the_flow(
    api_client, application, settings, kwargs, expected
):
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    db, order = pay_db(buyer)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, **kwargs)
    assert response.status_code == 202, response.text
    assert db.payments[0].phone_number == expected
    assert daraja.pushes[0]["phone"] == expected


@pytest.mark.parametrize(
    "body",
    [
        {"phone_number": "0712345678", "amount": 1},
        {"phone": "0712345678"},
        {"phone_number": "0" * 21},
        {"phone_number": "07"},
        {"phone_number": "   "},
        {"phone_number": 712345678},
        ["0712345678"],
        "0712345678",
        5,
    ],
)
async def test_unknown_fields_non_objects_and_bad_lengths_are_422(api_client, application, settings, body):
    db = FakeDB()
    daraja = FakeDaraja(db)
    install(application, db, make_buyer(), daraja)
    response = await post_pay(api_client, uuid4(), json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert db.statements == [] and daraja.pushes == []


async def test_a_body_that_is_not_json_is_422(api_client, application, settings):
    db = FakeDB()
    install(application, db, make_buyer(), FakeDaraja(db))
    response = await post_pay(
        api_client, uuid4(), content=b"phone=0712345678", headers={**PAY_HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert db.statements == []


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("0712345678", "254712345678"),
        ("0712 345 678", "254712345678"),
        ("0712-345-678", "254712345678"),
        ("+254712345678", "254712345678"),
        ("+254 712 345 678", "254712345678"),
        ("254712345678", "254712345678"),
        ("0112345678", "254112345678"),
        ("254112345678", "254112345678"),
        ("+254-112-345-678", "254112345678"),
    ],
)
def test_accepted_formats_normalise(raw, normalized):
    assert payment_service.resolve_payment_phone(make_buyer(), raw) == normalized


@pytest.mark.parametrize("raw", ["071234567", "07123456789", "07123456ab", "255712345678", "254612345678", "abc"])
async def test_invalid_numbers_are_422_invalid_phone(api_client, application, settings, raw):
    db = FakeDB()
    daraja = FakeDaraja(db)
    install(application, db, make_buyer(), daraja)
    response = await post_pay(api_client, uuid4(), json={"phone_number": raw})
    assert response.status_code == 422
    assert without_request_id(response.json()) == INVALID_PHONE
    assert db.statements == [] and daraja.pushes == []
    assert raw not in response.text


# Resolution ----------------------------------------------------------------------------------


def test_a_body_number_wins_over_the_profile_number():
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    assert payment_service.resolve_payment_phone(buyer, "0712345678") == ENTERED


def test_without_a_body_the_profile_number_is_used_and_revalidated():
    assert payment_service.resolve_payment_phone(make_buyer(phone_number=PROFILE_PHONE), None) == PROFILE_PHONE
    with pytest.raises(payment_service.AppError) as exc_info:
        payment_service.resolve_payment_phone(make_buyer(phone_number="255700000000"), None)
    assert exc_info.value.code == "invalid_phone"


@pytest.mark.parametrize("profile", ["missing", None, ""])
def test_with_neither_number_the_result_is_phone_required(profile):
    buyer = make_buyer()
    if profile == "missing":
        del buyer.phone_number  # an account after phone numbers are removed from users
    else:
        buyer.phone_number = profile
    with pytest.raises(payment_service.AppError) as exc_info:
        payment_service.resolve_payment_phone(buyer, None)
    assert (exc_info.value.status_code, exc_info.value.code) == (422, "phone_required")
    assert str(exc_info.value) == "Enter the M-Pesa number to send the payment prompt to."


async def test_a_buyer_without_a_number_gets_phone_required_before_any_lock(api_client, application, settings):
    buyer = make_buyer()
    del buyer.phone_number
    db = FakeDB(scalars=[None])
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, uuid4())
    assert response.status_code == 422
    assert without_request_id(response.json()) == {
        "error": {"code": "phone_required", "message": "Enter the M-Pesa number to send the payment prompt to."}
    }
    assert len(db.statements) == 1 and "FOR UPDATE" not in db.statements[0]
    assert daraja.pushes == []


async def test_a_buyer_without_a_profile_number_can_pay_with_an_entered_number(api_client, application, settings):
    buyer = make_buyer()
    del buyer.phone_number
    db, order = pay_db(buyer)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id, json={"phone_number": "0712345678"})
    assert response.status_code == 202, response.text
    assert (db.payments[0].phone_number, db.payments[0].stk_phone_number) == (ENTERED, ENTERED)


@pytest.mark.parametrize(
    ("target", "expected_stk"), [("entered", ENTERED), ("registered", ENTERED), ("test_msisdn", TEST_MSISDN)]
)
async def test_the_entered_number_is_stored_and_the_target_follows_the_setting(
    api_client, application, monkeypatch, target, expected_stk
):
    use_settings(monkeypatch, payment_settings(mpesa_stk_target=target))
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    db, order = pay_db(buyer)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, json={"phone_number": "0712 345 678"})
    assert response.status_code == 202, response.text
    payment = db.payments[0]
    assert payment.phone_number == ENTERED
    assert payment.stk_phone_number == expected_stk
    assert [push["phone"] for push in daraja.pushes] == [expected_stk]
    assert response.json()["prompt"] == f"Prompt sent to 254*******{expected_stk[-3:]}"
    # The entered number is never written to the user.
    assert buyer.phone_number == PROFILE_PHONE


@pytest.mark.parametrize(
    ("target", "expected_stk"), [("entered", ENTERED), ("registered", ENTERED), ("test_msisdn", TEST_MSISDN)]
)
async def test_daraja_receives_the_expected_party_a_and_phone_number(
    api_client, application, monkeypatch, target, expected_stk
):
    settings = use_settings(monkeypatch, payment_settings(mpesa_stk_target=target))
    sent: list[dict] = []

    def daraja_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "AT-test-token", "expires_in": "3599"})
        assert request.url.path == STK_PUSH_PATH
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "MerchantRequestID": "m-1",
                "CheckoutRequestID": "ws_CO_party_a_0001",
                "ResponseCode": "0",
                "ResponseDescription": "Success",
            },
        )

    client = DarajaClient(settings, transport=httpx.MockTransport(daraja_handler))
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    db, order = pay_db(buyer)
    install(application, db, buyer, client)
    try:
        response = await post_pay(api_client, order.id, json={"phone_number": "0712345678"})
    finally:
        await client.aclose()
    assert response.status_code == 202, response.text
    assert len(sent) == 1
    assert sent[0]["PartyA"] == sent[0]["PhoneNumber"] == expected_stk


# Replay --------------------------------------------------------------------------------------


def replay_db(buyer, order, existing, view=None):
    return FakeDB(scalars=[existing], results=[FakeResult(mapping_rows=[view or view_row(order)])])


def existing_payment(buyer, order, **overrides):
    values = {"idempotency_key": f"{buyer.id}:{order.id}:{PAY_KEY}", "phone_number": ENTERED}
    values.update(overrides)
    return make_payment(order, **values)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"json": {}}, {"json": {"phone_number": "0712345678"}}, {"json": {"phone_number": "+254 712 345 678"}}],
)
async def test_a_replay_without_a_number_or_with_the_same_number_is_202_without_daraja(
    api_client, application, settings, kwargs
):
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    order = make_order(buyer.id)
    db = replay_db(buyer, order, existing_payment(buyer, order))
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, **kwargs)
    assert response.status_code == 202, response.text
    assert daraja.pushes == [] and db.payments == []
    assert not statements_with(db, "pg_advisory_xact_lock")


async def test_a_replay_with_a_different_number_is_409(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = replay_db(buyer, order, existing_payment(buyer, order))
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, json={"phone_number": "0722000111"})
    assert response.status_code == 409
    assert without_request_id(response.json()) == {
        "error": {
            "code": "idempotency_key_reused",
            "message": "This request was already used with a different number. Start a new payment attempt.",
        }
    }
    assert daraja.pushes == [] and db.payments == []
    assert "254722000111" not in response.text and ENTERED not in response.text


async def test_an_invalid_number_in_a_replay_is_still_422(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = replay_db(buyer, order, existing_payment(buyer, order))
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id, json={"phone_number": "0612345678"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_phone"


@pytest.mark.parametrize("order_status", [OrderStatus.PAID, OrderStatus.EXPIRED, OrderStatus.CANCELLED])
async def test_a_replay_after_the_order_moved_on_is_safe(api_client, application, settings, order_status):
    buyer = make_buyer()
    order = make_order(buyer.id, status=order_status)
    existing = existing_payment(buyer, order, status=PaymentStatus.COMPLETED)
    view = view_row(order, status=str(order_status), payment_status="completed", payment_attempts=1)
    db = replay_db(buyer, order, existing, view)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, json={"phone_number": "0712345678"})
    assert response.status_code == 202, response.text
    assert response.json()["status"] == str(order_status)
    assert daraja.pushes == []


# Limits --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("starts", "window", "now", "expected"),
    [
        ([NOW - timedelta(minutes=20)], timedelta(hours=1), NOW, 2400),
        ([NOW - timedelta(minutes=59, seconds=30, microseconds=200000)], timedelta(hours=1), NOW, 30),
        ([NOW - timedelta(minutes=59, seconds=59, microseconds=999999)], timedelta(hours=1), NOW, 1),
        ([NOW - timedelta(hours=2)], timedelta(hours=1), NOW, 1),
        ([NOW], timedelta(hours=1), NOW, 3600),
        (
            [NOW - timedelta(hours=2), NOW - timedelta(hours=23), NOW - timedelta(hours=5)],
            timedelta(hours=24),
            NOW,
            3600,
        ),
        ([], timedelta(hours=1), NOW, 1),
    ],
)
def test_retry_after_is_whole_seconds_rounded_up_and_at_least_one(starts, window, now, expected):
    assert payment_service.retry_after_seconds(starts, window, now) == expected


async def test_the_per_number_limit_counts_the_entered_number_on_the_database_clock(api_client, application, settings):
    buyer = make_buyer()
    db_clock = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)  # far from the wall clock on purpose
    order = make_order(buyer.id, expires_at=db_clock + timedelta(minutes=5))
    oldest = db_clock - timedelta(minutes=59, seconds=30, microseconds=500000)
    db = FakeDB(scalars=[None, order, db_clock, 0, 5, oldest], lists=[[]])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id, json={"phone_number": "0733444555"})
    assert response.status_code == 429
    assert without_request_id(response.json()) == {
        "error": {
            "code": "too_many_payment_attempts",
            "message": "Too many payment attempts for this phone number. Please try again later.",
        }
    }
    assert response.headers["Retry-After"] == "30"
    count = db.raw_statements[-2]
    assert "254733444555" in bound_values(count)
    assert "payments.phone_number = " in db.statements[-2] and "now()" in db.statements[-2]


async def test_test_msisdn_mode_does_not_let_two_buyers_block_each_other(api_client, application, monkeypatch):
    # The old rule counted stk_phone_number, which is the same test number for every payment in this mode.
    use_settings(monkeypatch, payment_settings(mpesa_stk_target="test_msisdn", mpesa_phone_attempts_per_hour=1))
    results = []
    for number, normalized in (("0711000001", "254711000001"), ("0711000002", "254711000002")):
        buyer = make_buyer()
        db, order = pay_db(buyer)
        install(application, db, buyer, FakeDaraja(db))
        response = await post_pay(api_client, order.id, json={"phone_number": number})
        index = statements_with(db, "payments.phone_number = ")[0]
        per_number = bound_values(db.raw_statements[index])
        results.append((response.status_code, db.payments[0].stk_phone_number, normalized in per_number))
        assert TEST_MSISDN not in per_number
    assert results == [(202, TEST_MSISDN, True), (202, TEST_MSISDN, True)]


async def test_the_per_buyer_hourly_limit_has_its_own_code_and_retry_after(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    oldest = NOW - timedelta(minutes=45)
    db = FakeDB(scalars=[None, order, NOW, 0, 0, 10, oldest], lists=[[]])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 429
    assert without_request_id(response.json()) == {
        "error": {
            "code": "too_many_buyer_payment_attempts",
            "message": "You have made too many payment attempts. Please try again later.",
        }
    }
    assert response.headers["Retry-After"] == str(15 * 60)
    assert "orders.buyer_id = " in db.statements[-2]
    assert buyer.id in bound_values(db.raw_statements[-2])
    assert db.payments == []


USED_TODAY = [
    ("254711000001", NOW - timedelta(hours=2)),
    ("254711000002", NOW - timedelta(hours=23, minutes=30)),
    ("254711000003", NOW - timedelta(hours=10)),
]


async def test_the_distinct_numbers_limit_has_its_own_code_and_retry_after(api_client, application, settings):
    buyer = make_buyer()
    db, order = pay_db(buyer, used_phones=USED_TODAY)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, json={"phone_number": "0711000004"})
    assert response.status_code == 429
    assert without_request_id(response.json()) == {
        "error": {
            "code": "too_many_phone_numbers",
            "message": "You have used too many different phone numbers today. "
            "Please try again tomorrow or use one you already used.",
        }
    }
    # The second number drops out of the 24 hour window first, 30 minutes from now.
    assert response.headers["Retry-After"] == str(30 * 60)
    assert db.payments == [] and daraja.pushes == []
    assert "GROUP BY payments.phone_number" in db.statements[-1]


async def test_a_number_already_used_today_is_allowed_at_the_distinct_limit(api_client, application, settings):
    buyer = make_buyer()
    db, order = pay_db(buyer, used_phones=USED_TODAY)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id, json={"phone_number": "0711 000 002"})
    assert response.status_code == 202, response.text
    assert db.payments[0].phone_number == "254711000002"


@pytest.mark.parametrize(
    ("scalars", "code"),
    [
        ([None, make_order(uuid4(), status=OrderStatus.PAID)], "order_not_payable"),
        ([None, make_order(uuid4(), expires_at=NOW + timedelta(seconds=30)), NOW], "order_expiring"),
    ],
)
async def test_an_earlier_business_error_neither_consumes_nor_reveals_a_limit(
    api_client, application, settings, scalars, code
):
    buyer = make_buyer()
    db = FakeDB(scalars=list(scalars))
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, scalars[1].id, json={"phone_number": "0712345678"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    assert "Retry-After" not in response.headers
    assert not statements_with(db, "count(payments.id)")
    assert db.payments == []


# Locking -------------------------------------------------------------------------------------


async def test_the_advisory_lock_comes_first_no_listing_is_locked_and_commit_precedes_daraja(
    api_client, application, settings
):
    buyer = make_buyer()
    db, order = pay_db(buyer)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id, json={"phone_number": "0712345678"})
    assert response.status_code == 202, response.text
    (advisory,) = statements_with(db, "pg_advisory_xact_lock(hashtext(")
    order_lock = next(i for i, sql in enumerate(db.statements) if "FROM orders" in sql and "FOR UPDATE" in sql)
    assert advisory < order_lock
    assert bound_values(db.raw_statements[advisory]) == [f"pay-buyer:{buyer.id}"]
    assert not any("produce_listings" in sql and "FOR UPDATE" in sql.upper() for sql in db.statements)
    assert daraja.open_during_push == []
    # Every limit is read after the order lock and before the insert.
    for fragment in ("count(payments.id)", "GROUP BY payments.phone_number"):
        assert all(index > order_lock for index in statements_with(db, fragment))


# Responses, events and logs ------------------------------------------------------------------


async def test_numbers_are_masked_in_the_response_events_and_logs(api_client, application, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    use_settings(monkeypatch, payment_settings(mpesa_stk_target="test_msisdn"))
    buyer = make_buyer(phone_number=PROFILE_PHONE)
    order = make_order(buyer.id)
    view = view_row(
        order,
        payment_status="initiated",
        payment_phone_number="254733444555",
        payment_stk_phone_number=TEST_MSISDN,
        payment_attempts=1,
        has_open_payment=True,
    )
    db, _ = pay_db(buyer, order, view=view)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id, json={"phone_number": "0733 444 555"})
    assert response.status_code == 202, response.text
    payment = response.json()["payment"]
    assert payment["phone_masked"] == "254*******555"
    assert payment["stk_target_masked"] == "254*******149"
    haystacks = {
        "response": response.text,
        "events": json.dumps([event.payload for event in db.events]),
        "logs": caplog.text,
    }
    for name, haystack in haystacks.items():
        for raw in ("254733444555", "0733444555", "0733 444 555", PROFILE_PHONE, TEST_MSISDN):
            assert raw not in haystack, f"{raw} leaked into {name}"


async def test_phone_masked_is_null_when_there_is_no_payment(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    # The order, then reconciliation finds no payment, then the order view.
    db = FakeDB(scalars=[order], results=[FakeResult(), FakeResult(mapping_rows=[view_row(order)])])
    install(application, db, buyer, FakeDaraja(db))
    response = await api_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200, response.text
    assert response.json()["payment"] is None


async def test_the_buyer_order_view_masks_both_numbers(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    view = view_row(
        order,
        payment_status="failed",
        payment_phone_number=BUYER_PHONE,
        payment_stk_phone_number=TEST_MSISDN,
        payment_attempts=1,
    )
    # The latest payment failed, so reconciliation reads it and stops; then the order view.
    failed = make_payment(order, status=PaymentStatus.FAILED)
    latest = FakeResult(rows=[(failed.id, failed.status, failed.checkout_request_id, failed.created_at, NOW)])
    db = FakeDB(scalars=[order], results=[latest, FakeResult(mapping_rows=[view])])
    install(application, db, buyer, FakeDaraja(db))
    response = await api_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200, response.text
    payment = response.json()["payment"]
    assert (payment["phone_masked"], payment["stk_target_masked"]) == ("254*******678", "254*******149")
    assert BUYER_PHONE not in response.text and TEST_MSISDN not in response.text


# Source checks -------------------------------------------------------------------------------

TOUCHED_FILES = [
    "services/payment_service.py",
    "services/order_service.py",
    "services/admin_order_service.py",
    "routers/orders.py",
    "schemas/payment.py",
    "core/errors.py",
    "utils/phone.py",
]


@pytest.mark.parametrize("name", TOUCHED_FILES)
def test_touched_files_never_use_float(name):
    assert re.search(r"\bfloat\b", (APP_DIR / name).read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("name", TOUCHED_FILES)
def test_touched_files_build_no_sql_with_string_formatting(name):
    source = (APP_DIR / name).read_text(encoding="utf-8")
    # SQL is built from constants and bound parameters only (a bound value may itself be an f-string).
    assert re.search(r"\btext\(\s*f[\"']", source) is None
    assert re.search(r"\btext\([^)]*\.format\(", source) is None
    assert re.search(r"\btext\([^)]*%\s*\(", source) is None
    assert re.search(r"\bexec_driver_sql\(", source) is None


def test_the_payment_service_never_logs_a_phone_number():
    source = (APP_DIR / "services/payment_service.py").read_text(encoding="utf-8")
    log_calls = re.findall(r"logger\.\w+\((.*?)\)\n", source, flags=re.DOTALL)
    assert log_calls
    for call in log_calls:
        assert "phone" not in call.lower()
