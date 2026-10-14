"""Module 5 payment tests without a database or network: pay endpoint, results, callbacks, reconciliation."""

import json
import logging
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from payment_fakes import (
    ACCEPTED_PUSH,
    BUYER_PHONE,
    CALLBACK_TOKEN,
    NOW,
    REJECTED_PUSH,
    SECRETS,
    TEST_MSISDN,
    FakeDaraja,
    FakeDB,
    FakeResult,
    callback_body,
    make_buyer,
    make_order,
    make_payment,
    payment_settings,
    query_result,
    use_settings,
    view_row,
)
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_current_user
from app.db import get_db, get_session_factory
from app.integrations.daraja import (
    SANDBOX_BASE_URL,
    STK_PUSH_PATH,
    DarajaAuthError,
    DarajaClient,
    DarajaRequestError,
    DarajaTimeout,
    get_daraja_client,
)
from app.models.enums import OrderStatus, PaymentStatus, UserRole
from app.services import payment_service

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PAY_KEY = "pay-key-0001"
PAY_HEADERS = {"Idempotency-Key": PAY_KEY}
CHECKOUT_ID = ACCEPTED_PUSH.checkout_request_id
ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}


@pytest.fixture
def settings(monkeypatch):
    return use_settings(monkeypatch, payment_settings())


@pytest.fixture
def rematch_calls(monkeypatch):
    calls = []

    async def fake_rematch(session_factory, checkout_request_id):
        del session_factory
        calls.append(checkout_request_id)

    monkeypatch.setattr(payment_service, "rematch_orphan_events", fake_rematch)
    return calls


def install(application, db: FakeDB, user=None, client=None) -> None:
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user

    async def request_session():
        yield db.session()

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_session_factory] = lambda: db.factory
    application.dependency_overrides[get_daraja_client] = lambda: client


def pay_db(buyer, order=None, *, db_now=NOW, open_payments=(), attempts=0, recent=0, view=None, lists=()):
    """Script a full pay request: phase 1 checks and insert, phase 3 locks, and the final order view."""
    order = order or make_order(buyer.id)
    db = FakeDB(
        scalars=[None, order, db_now, attempts, recent, order, lambda db: db.payments[-1]],
        lists=[list(open_payments), *lists],
        results=[FakeResult(mapping_rows=[view or view_row(order)])],
    )
    return db, order


async def post_pay(api_client, order_id, headers=PAY_HEADERS, **kwargs):
    return await api_client.post(f"/api/v1/orders/{order_id}/pay", headers=headers, **kwargs)


def lock_index(statements, table):
    return next(i for i, sql in enumerate(statements) if f"FROM {table}" in sql and "FOR UPDATE" in sql)


# Pay endpoint: access and input --------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.ADMIN])
async def test_farmer_and_admin_cannot_pay(api_client, application, settings, role):
    db = FakeDB()
    daraja = FakeDaraja(db)
    install(application, db, make_buyer(role=role), daraja)
    response = await post_pay(api_client, uuid4())
    assert response.status_code == 403
    assert daraja.pushes == []


async def test_pay_without_a_token_is_401(api_client, application, settings):
    install(application, FakeDB(), None, FakeDaraja())
    response = await post_pay(api_client, uuid4())
    assert response.status_code == 401


async def test_another_buyers_order_is_the_same_404_as_a_missing_one(api_client, application, settings):
    db = FakeDB(scalars=[None, None, None, None])
    daraja = FakeDaraja(db)
    install(application, db, make_buyer(), daraja)
    foreign = await post_pay(api_client, uuid4())
    missing = await post_pay(api_client, uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"error": {"code": "order_not_found", "message": "Order not found."}}
    order_locks = [sql for sql in db.statements if "FROM orders" in sql]
    assert order_locks and all("orders.buyer_id = " in sql and "FOR UPDATE" in sql for sql in order_locks)
    assert daraja.pushes == []


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "idempotency_key_required"),
        ({"Idempotency-Key": ""}, "idempotency_key_required"),
        ({"Idempotency-Key": "short"}, "invalid_idempotency_key"),
        ({"Idempotency-Key": "has spaces in it"}, "invalid_idempotency_key"),
        ({"Idempotency-Key": "semi;colon;key"}, "invalid_idempotency_key"),
        ({"Idempotency-Key": "k" * 65}, "invalid_idempotency_key"),
    ],
)
async def test_missing_or_invalid_idempotency_key_is_422(api_client, application, settings, headers, code):
    db = FakeDB()
    install(application, db, make_buyer(), FakeDaraja(db))
    response = await post_pay(api_client, uuid4(), headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert db.statements == []


async def test_a_request_body_is_rejected(api_client, application, settings):
    db = FakeDB()
    daraja = FakeDaraja(db)
    install(application, db, make_buyer(), daraja)
    response = await post_pay(api_client, uuid4(), json={"amount": 1, "phone_number": "254700000001"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "body_not_allowed"
    assert db.statements == [] and daraja.pushes == []


def test_stored_key_is_scoped_to_buyer_and_order():
    buyer = make_buyer()
    order_id = uuid4()
    assert payment_service.stored_idempotency_key(buyer, order_id, PAY_KEY) == f"{buyer.id}:{order_id}:{PAY_KEY}"


async def test_replay_returns_202_without_calling_daraja_even_after_the_order_is_paid(
    api_client, application, settings
):
    buyer = make_buyer()
    order = make_order(buyer.id, status=OrderStatus.PAID)
    existing = make_payment(order, status=PaymentStatus.COMPLETED, idempotency_key=f"{buyer.id}:{order.id}:{PAY_KEY}")
    view = view_row(
        order, status="paid", payment_status="completed", payment_stk_phone_number=BUYER_PHONE, payment_attempts=1
    )
    db = FakeDB(scalars=[existing], results=[FakeResult(mapping_rows=[view])])
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "paid"
    assert response.json()["prompt"] == "Prompt sent to 254*******678"
    assert daraja.pushes == []
    assert "payments.idempotency_key = " in db.statements[0]
    assert db.payments == []


# Pay endpoint: refusals ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.EXPIRED, OrderStatus.CANCELLED])
async def test_order_that_is_not_pending_is_409(api_client, application, settings, status):
    buyer = make_buyer()
    order = make_order(buyer.id, status=status)
    db = FakeDB(scalars=[None, order])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_payable"


@pytest.mark.parametrize(
    ("expires_in", "reason"), [(timedelta(seconds=89), "too_close"), (timedelta(seconds=-1), "expired")]
)
async def test_order_with_less_than_90_seconds_left_is_409(api_client, application, settings, expires_in, reason):
    buyer = make_buyer()
    order = make_order(buyer.id, expires_at=NOW + expires_in)
    db = FakeDB(scalars=[None, order, NOW])
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_expiring"
    assert response.json()["error"]["reason"] == reason
    assert daraja.pushes == []


@pytest.mark.parametrize(
    "open_payment",
    [
        {"status": PaymentStatus.INITIATED},
        {"status": PaymentStatus.INITIATING, "checkout_request_id": None, "created_at": NOW - timedelta(seconds=10)},
    ],
)
async def test_an_open_payment_is_409(api_client, application, settings, open_payment):
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = FakeDB(scalars=[None, order, NOW], lists=[[make_payment(order, **open_payment)]])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_in_progress"
    assert db.payments == []


async def test_three_attempts_are_the_maximum(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = FakeDB(scalars=[None, order, NOW, 3], lists=[[]])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "max_attempts_reached"


async def test_five_attempts_on_one_phone_in_an_hour_is_429(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = FakeDB(scalars=[None, order, NOW, 0, 5], lists=[[]])
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_payment_attempts"
    assert "payments.stk_phone_number = " in db.statements[-1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"mpesa_consumer_key": ""},
        {"mpesa_passkey": ""},
        {"mpesa_callback_token": ""},
        {"mpesa_callback_base_url": ""},
        {"mpesa_callback_base_url": "http://insecure.example.test"},
    ],
)
async def test_missing_payment_settings_give_503(api_client, application, monkeypatch, overrides):
    use_settings(monkeypatch, payment_settings(**overrides))
    db = FakeDB()
    install(application, db, make_buyer(), FakeDaraja(db))
    response = await post_pay(api_client, uuid4())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payments_unavailable"
    assert db.statements == []


async def test_missing_daraja_client_gives_503(api_client, application, settings):
    install(application, FakeDB(), make_buyer(), None)
    response = await post_pay(api_client, uuid4())
    assert response.status_code == 503


# Pay endpoint: success and failure -----------------------------------------------------------


async def test_a_stale_initiating_payment_is_failed_and_a_new_attempt_succeeds(
    api_client, application, settings, rematch_calls
):
    buyer = make_buyer()
    order = make_order(buyer.id)
    stale = make_payment(
        order,
        status=PaymentStatus.INITIATING,
        checkout_request_id=None,
        merchant_request_id=None,
        created_at=NOW - timedelta(seconds=61),
    )
    db, _ = pay_db(buyer, order, open_payments=[stale], attempts=1)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text
    assert (stale.status, stale.result_desc) == (PaymentStatus.FAILED, "request_not_sent")
    assert db.payments[-1].status == PaymentStatus.INITIATED


async def test_a_new_attempt_is_allowed_after_a_cancelled_payment(api_client, application, settings, rematch_calls):
    buyer = make_buyer()
    db, order = pay_db(buyer, attempts=1)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize(("target", "expected_phone"), [("registered", BUYER_PHONE), ("test_msisdn", TEST_MSISDN)])
async def test_payment_values_come_from_the_server(
    api_client, application, monkeypatch, rematch_calls, target, expected_phone
):
    use_settings(monkeypatch, payment_settings(mpesa_stk_target=target))
    buyer = make_buyer()
    db, order = pay_db(buyer)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text
    payment = db.payments[0]
    assert payment.amount == order.total_amount == 114
    assert payment.phone_number == BUYER_PHONE
    assert payment.stk_phone_number == expected_phone
    assert payment.environment == "sandbox"
    assert payment.idempotency_key == f"{buyer.id}:{order.id}:{PAY_KEY}"
    assert daraja.pushes == [
        {
            "phone": expected_phone,
            "amount": 114,
            "account_reference": order.reference,
            "callback_url": f"https://payments.example.test/api/v1/payments/callback/{CALLBACK_TOKEN}",
        }
    ]
    assert response.json()["prompt"] == f"Prompt sent to 254*******{expected_phone[-3:]}"


async def test_accepted_push_initiates_the_payment(api_client, application, settings, rematch_calls):
    buyer = make_buyer()
    db, order = pay_db(buyer)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message"] == "Check your phone and enter your M-Pesa PIN."
    assert body["prompt"] == "Prompt sent to 254*******678"
    assert body["id"] == str(order.id)
    payment = db.payments[0]
    assert payment.status == PaymentStatus.INITIATED
    assert payment.checkout_request_id == CHECKOUT_ID
    assert payment.merchant_request_id == ACCEPTED_PUSH.merchant_request_id
    assert [event.event_type for event in db.events] == ["stk_request", "stk_response"]
    assert all(event.payment_id == payment.id for event in db.events)
    assert BUYER_PHONE not in json.dumps([event.payload for event in db.events])
    assert rematch_calls == [CHECKOUT_ID]
    for forbidden in ("checkout_request_id", "merchant_request_id", "idempotency", CHECKOUT_ID, BUYER_PHONE):
        assert forbidden not in response.text


@pytest.mark.parametrize(
    ("daraja_kwargs", "result_desc"),
    [
        ({"push": REJECTED_PUSH}, "request_rejected"),
        ({"push_error": DarajaRequestError("Daraja STK request failed (HTTP 500)")}, "request_rejected"),
        ({"push_error": DarajaAuthError("Daraja rejected the access token")}, "request_rejected"),
        ({"push_error": DarajaTimeout("Daraja did not respond in time")}, "request_unconfirmed"),
    ],
)
async def test_rejected_or_unconfirmed_push_fails_the_payment(
    api_client, application, settings, rematch_calls, daraja_kwargs, result_desc
):
    buyer = make_buyer()
    db, order = pay_db(buyer)
    install(application, db, buyer, FakeDaraja(db, **daraja_kwargs))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "payment_request_failed",
            "message": "Could not start the M-Pesa prompt. Please try again.",
        }
    }
    payment = db.payments[0]
    assert (payment.status, payment.result_desc) == (PaymentStatus.FAILED, result_desc)
    assert payment.checkout_request_id is None and payment.merchant_request_id is None
    assert [event.event_type for event in db.events] == ["stk_request", "error"]
    assert db.events[-1].payload["reason"] == result_desc
    assert rematch_calls == []


async def test_no_transaction_is_open_during_the_daraja_call(api_client, application, settings, rematch_calls):
    buyer = make_buyer()
    db, order = pay_db(buyer)
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202
    assert daraja.open_during_push == []
    assert db.commits >= 2  # phase 1 and the stk_request event were committed before the call


async def test_pay_locks_the_order_before_payments_and_never_a_listing(
    api_client, application, settings, rematch_calls
):
    buyer = make_buyer()
    db, order = pay_db(buyer)
    install(application, db, buyer, FakeDaraja(db))
    await post_pay(api_client, order.id)
    statements = db.statements
    order_lock = lock_index(statements, "orders")
    payment_lock = lock_index(statements, "payments")
    assert order_lock < payment_lock
    # The final order view joins listings to show them, but nothing in the pay flow locks a listing.
    assert not any("produce_listings" in sql and "FOR UPDATE" in sql.upper() for sql in statements)


async def test_a_concurrent_insert_rejected_by_the_partial_index_is_409(api_client, application, settings):
    buyer = make_buyer()
    order = make_order(buyer.id)
    error = IntegrityError(
        "insert into payments", {}, Exception('duplicate key value violates "payments_one_open_per_order_idx"')
    )
    db = FakeDB(scalars=[None, order, NOW, 0, 0], lists=[[]], commit_errors=[error])
    daraja = FakeDaraja(db)
    install(application, db, buyer, daraja)
    response = await post_pay(api_client, order.id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_in_progress"
    assert daraja.pushes == []


# Applying results ----------------------------------------------------------------------------


def apply_db(order, payment, times=1):
    ids = FakeResult(rows=[(payment.id, order.id)])
    return FakeDB(results=[ids] * times, scalars=[order, payment] * times)


async def apply(db, outcome="success", **overrides):
    values = {"result_code": 0, "result_desc": "ok", "receipt": "SBX1A2B3C4", "amount": 114, "source": "callback"}
    values.update(overrides)
    return await payment_service.apply_payment_result(db.factory, CHECKOUT_ID, outcome, **values)


def pending_pair(**payment_overrides):
    order = make_order(uuid4())
    return order, make_payment(order, **payment_overrides)


async def test_success_completes_the_payment_and_pays_the_order():
    order, payment = pending_pair()
    db = apply_db(order, payment)
    assert await apply(db) == "applied"
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.mpesa_receipt_number == "SBX1A2B3C4"
    assert payment.result_code == 0
    assert payment.completed_at is not None and payment.callback_received_at is not None
    assert order.status == OrderStatus.PAID
    assert db.events == []


@pytest.mark.parametrize("amount", [114, Decimal("114.00"), "114", "114.0"])
async def test_equal_amounts_in_any_form_are_accepted(amount):
    order, payment = pending_pair()
    assert await apply(apply_db(order, payment), amount=amount) == "applied"
    assert order.status == OrderStatus.PAID


async def test_the_same_result_twice_changes_nothing_the_second_time():
    order, payment = pending_pair()
    db = apply_db(order, payment, times=2)
    assert await apply(db) == "applied"
    before = (payment.status, payment.mpesa_receipt_number, payment.result_desc, order.status)
    assert await apply(db, receipt="OTHER", result_desc="changed") == "duplicate"
    assert (payment.status, payment.mpesa_receipt_number, payment.result_desc, order.status) == before


@pytest.mark.parametrize("amount", [113, "113.99", "not-a-number"])
async def test_amount_mismatch_never_pays_the_order(amount):
    order, payment = pending_pair()
    db = apply_db(order, payment)
    assert await apply(db, amount=amount) == "mismatch"
    assert (payment.status, payment.result_desc) == (PaymentStatus.FAILED, "amount_mismatch")
    assert payment.mpesa_receipt_number is None
    assert order.status == OrderStatus.NEEDS_RECONCILIATION
    assert [event.payload["reason"] for event in db.events_of("error")] == ["amount_mismatch"]


@pytest.mark.parametrize(
    ("outcome", "code", "status"),
    [
        ("cancelled", 1032, PaymentStatus.CANCELLED),
        ("timeout", 1037, PaymentStatus.TIMEOUT),
        ("failed", 2001, PaymentStatus.FAILED),
    ],
)
async def test_unsuccessful_results_leave_the_order_payable(outcome, code, status):
    order, payment = pending_pair()
    db = apply_db(order, payment)
    assert await apply(db, outcome, result_code=code, receipt=None, amount=None) == "applied"
    assert (payment.status, payment.result_code) == (status, code)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert not any("produce_listings" in sql for sql in db.statements)


async def test_unknown_checkout_id_is_unmatched():
    db = FakeDB(results=[FakeResult()])
    assert await apply(db) == "unmatched"


async def test_late_success_on_an_expired_order_needs_reconciliation():
    order, payment = pending_pair(status=PaymentStatus.TIMEOUT)
    order.status = OrderStatus.EXPIRED
    assert await apply(apply_db(order, payment)) == "applied"
    assert payment.status == PaymentStatus.COMPLETED
    assert order.status == OrderStatus.NEEDS_RECONCILIATION


async def test_a_non_success_result_for_a_timed_out_payment_is_a_duplicate():
    order, payment = pending_pair(status=PaymentStatus.TIMEOUT)
    assert await apply(apply_db(order, payment), "cancelled", result_code=1032) == "duplicate"
    assert payment.status == PaymentStatus.TIMEOUT


@pytest.mark.parametrize("closed", [PaymentStatus.FAILED, PaymentStatus.CANCELLED])
async def test_success_after_a_closed_payment_needs_reconciliation(closed):
    order, payment = pending_pair(status=closed)
    db = apply_db(order, payment)
    assert await apply(db) == "duplicate"
    assert payment.status == closed and payment.mpesa_receipt_number is None
    assert order.status == OrderStatus.NEEDS_RECONCILIATION
    assert [event.payload["reason"] for event in db.events_of("error")] == ["late_success_on_closed_payment"]


async def test_success_after_a_closed_payment_never_changes_a_paid_order():
    order, payment = pending_pair(status=PaymentStatus.FAILED)
    order.status = OrderStatus.PAID
    assert await apply(apply_db(order, payment)) == "duplicate"
    assert order.status == OrderStatus.PAID


async def test_query_results_do_not_set_callback_received_at():
    order, payment = pending_pair()
    assert await apply(apply_db(order, payment), receipt=None, amount=None, source="query") == "applied"
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.callback_received_at is None


async def test_an_integrity_error_is_reported_as_a_duplicate_and_logged():
    order, payment = pending_pair()
    db = apply_db(order, payment)
    db.commit_errors = [IntegrityError("update payments", {}, Exception("payments_receipt_unique_idx"))]
    assert await apply(db) == "duplicate"
    assert db.rollbacks == 1
    assert len(db.events_of("error")) == 1


async def test_apply_locks_the_order_before_the_payment():
    order, payment = pending_pair()
    db = apply_db(order, payment)
    await apply(db)
    assert "FOR UPDATE" not in db.statements[0]
    assert lock_index(db.statements, "orders") < lock_index(db.statements, "payments")
    assert not any("produce_listings" in sql for sql in db.statements)


# Callback processing -------------------------------------------------------------------------


async def test_a_matched_callback_is_recorded_attached_and_applied():
    order, payment = pending_pair()
    db = FakeDB(
        scalars=[payment.id, order, payment],
        results=[FakeResult(), FakeResult(rows=[(payment.id, order.id)])],
    )
    result = await payment_service.process_callback(db.factory, json.dumps(callback_body()).encode())
    assert result == "applied"
    assert payment.status == PaymentStatus.COMPLETED and order.status == OrderStatus.PAID
    event = db.events_of("callback")[0]
    assert event.checkout_request_id == CHECKOUT_ID and event.payment_id is None
    attach = next(sql for sql in db.statements if sql.startswith("UPDATE payment_events"))
    assert "payment_events.payment_id IS NULL" in attach


async def test_a_callback_before_the_ids_were_saved_is_kept_and_rematched():
    order, payment = pending_pair()
    db = FakeDB(scalars=[None])
    assert await payment_service.process_callback(db.factory, json.dumps(callback_body()).encode()) == "unmatched"
    orphan = db.events_of("callback")[0]
    assert orphan.payment_id is None

    db.scalar_queue = [payment.id, order, payment]
    db.result_queue = [
        FakeResult(rows=[(NOW, orphan.id, orphan.payload)]),
        FakeResult(rows=[(payment.id, order.id)]),
    ]
    assert await payment_service.rematch_orphan_events(db.factory, CHECKOUT_ID) == "applied"
    attach = next(sql for sql in db.statements if sql.startswith("UPDATE payment_events"))
    assert "payment_events.payment_id IS NULL" in attach
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.mpesa_receipt_number == "SBX1A2B3C4"
    assert order.status == OrderStatus.PAID


async def test_rematch_applies_the_newest_attached_callback():
    order, payment = pending_pair()
    cancelled = payment_service._capped(callback_body(result_code=1032))
    paid = payment_service._capped(callback_body())
    db = FakeDB(
        scalars=[payment.id, order, payment],
        results=[
            FakeResult(rows=[(NOW, uuid4(), paid), (NOW - timedelta(seconds=5), uuid4(), cancelled)]),
            FakeResult(rows=[(payment.id, order.id)]),
        ],
    )
    assert await payment_service.rematch_orphan_events(db.factory, CHECKOUT_ID) == "applied"
    assert payment.status == PaymentStatus.COMPLETED


async def test_rematch_skips_events_the_callback_route_already_attached():
    # The callback route won the race: no rows still have a null payment_id, so nothing is re-applied.
    order, payment = pending_pair()
    db = FakeDB(scalars=[payment.id], results=[FakeResult()])
    assert await payment_service.rematch_orphan_events(db.factory, CHECKOUT_ID) is None
    assert payment.status == PaymentStatus.INITIATED


async def test_a_rematch_failure_does_not_fail_an_accepted_payment(api_client, application, settings, monkeypatch):
    async def failing_rematch(session_factory, checkout_request_id):
        raise RuntimeError("payment events are append-only")

    monkeypatch.setattr(payment_service, "rematch_orphan_events", failing_rematch)
    buyer = make_buyer()
    db, order = pay_db(buyer)
    install(application, db, buyer, FakeDaraja(db))
    response = await post_pay(api_client, order.id)
    assert response.status_code == 202, response.text
    assert db.payments[0].status == PaymentStatus.INITIATED
    assert db.events[-1].payload["reason"] == "orphan_rematch_failed"


async def test_rematch_does_nothing_before_the_payment_has_its_checkout_id():
    db = FakeDB(scalars=[None])
    assert await payment_service.rematch_orphan_events(db.factory, CHECKOUT_ID) is None


async def test_the_callback_phone_number_is_never_used_for_lookup():
    order, payment = pending_pair()
    body = callback_body(checkout_request_id="ws_CO_unknown")
    body["Body"]["stkCallback"]["CallbackMetadata"]["Item"][0]["Value"] = int(BUYER_PHONE)
    db = FakeDB(scalars=[None])
    assert await payment_service.process_callback(db.factory, json.dumps(body).encode()) == "unmatched"
    assert payment.status == PaymentStatus.INITIATED and order.status == OrderStatus.PENDING_PAYMENT
    assert not any("phone" in sql for sql in db.statements)
    assert BUYER_PHONE not in json.dumps(db.events[0].payload)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{not json", "malformed_json"),
        (b"[1, 2, 3]", "unexpected_callback_shape"),
        (b'{"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1"}}}', "unexpected_callback_shape"),
        (b'{"Body": "not-an-object"}', "unexpected_callback_shape"),
    ],
)
async def test_a_malformed_callback_logs_one_error_event(raw, reason):
    db = FakeDB()
    assert await payment_service.process_callback(db.factory, raw) is None
    assert [(event.event_type, event.payload["reason"]) for event in db.events] == [("error", reason)]
    assert db.statements == []


def test_parse_callback_matches_metadata_by_name_and_ignores_the_phone():
    parsed = payment_service.parse_callback(callback_body(amount=114, receipt="SBXREC"))
    assert parsed is not None
    assert (parsed.checkout_request_id, parsed.result_code, parsed.outcome) == (CHECKOUT_ID, 0, "success")
    assert (parsed.amount, parsed.receipt) == (114, "SBXREC")
    assert "Phone" not in repr(parsed)


# Callback endpoint ---------------------------------------------------------------------------


def callback_url(token=CALLBACK_TOKEN):
    return f"/api/v1/payments/callback/{token}"


async def test_wrong_callback_token_is_an_empty_404_and_writes_nothing(api_client, application, settings):
    db = FakeDB()
    install(application, db)
    response = await api_client.post(callback_url("wrong-token-wrong-token-1234"), json=callback_body())
    assert response.status_code == 404
    assert response.content == b""
    assert db.added == [] and db.statements == []


async def test_oversize_callback_is_413(api_client, application, settings):
    db = FakeDB()
    install(application, db)
    response = await api_client.post(callback_url(), content=b"x" * 16385)
    assert response.status_code == 413
    assert db.added == []


def _known_payment_db(payment_status=PaymentStatus.INITIATED):
    order, payment = pending_pair(status=payment_status)
    return FakeDB(
        scalars=[payment.id, order, payment],
        results=[FakeResult(), FakeResult(rows=[(payment.id, order.id)])],
    )


@pytest.mark.parametrize(
    ("make_db", "content"),
    [
        (FakeDB, b"{not json"),
        (lambda: FakeDB(scalars=[None]), json.dumps(callback_body()).encode()),
        (lambda: _known_payment_db(PaymentStatus.COMPLETED), json.dumps(callback_body()).encode()),
        (_known_payment_db, json.dumps(callback_body(amount=1)).encode()),
        (FakeDB, json.dumps(callback_body()).encode()),  # unscripted lookup: an internal error
    ],
    ids=["malformed", "unknown", "duplicate", "mismatch", "internal-error"],
)
async def test_a_valid_token_always_gets_the_acknowledgement(api_client, application, settings, make_db, content):
    install(application, make_db())
    response = await api_client.post(callback_url(), content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json() == ACK


def test_access_log_filter_hides_the_callback_token(application):
    from app.main import CallbackTokenFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "POST", f"/api/v1/payments/callback/{CALLBACK_TOKEN}", "1.1", 200),
        None,
    )
    assert CallbackTokenFilter().filter(record) is True
    message = record.getMessage()
    assert CALLBACK_TOKEN not in message
    assert "/api/v1/payments/callback/*** HTTP/1.1" in message
    installed = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(item, CallbackTokenFilter) for item in installed)


# Reconciliation ------------------------------------------------------------------------------


def latest(payment, db_now=NOW):
    return FakeResult(rows=[(payment.id, payment.status, payment.checkout_request_id, payment.created_at, db_now)])


def due_pair(status=PaymentStatus.INITIATED, age=timedelta(seconds=120)):
    buyer = make_buyer()
    order = make_order(buyer.id)
    return buyer, order, make_payment(order, status=status, created_at=NOW - age)


async def test_pending_query_changes_nothing(settings, rematch_calls):
    buyer, order, payment = due_pair()
    db = FakeDB(results=[latest(payment)], scalars=[PaymentStatus.INITIATED, None])
    daraja = FakeDaraja(query=query_result("pending", None))
    await payment_service.maybe_reconcile_payment(db.factory, order, buyer, daraja)
    assert daraja.queries == [CHECKOUT_ID]
    assert rematch_calls == [CHECKOUT_ID]
    assert payment.status == PaymentStatus.INITIATED
    assert [event.event_type for event in db.events] == ["status_query"]
    assert db.result_queue == [] and db.scalar_queue == []


async def test_query_success_completes_without_receipt_or_amount_check(settings, rematch_calls):
    buyer, order, payment = due_pair()
    db = FakeDB(
        results=[latest(payment), FakeResult(rows=[(payment.id, order.id)])],
        scalars=[PaymentStatus.INITIATED, None, order, payment],
    )
    daraja = FakeDaraja(query=query_result("success", 0))
    await payment_service.maybe_reconcile_payment(db.factory, order, buyer, daraja)
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.mpesa_receipt_number is None
    assert payment.callback_received_at is None
    assert order.status == OrderStatus.PAID
    assert db.events_of("error") == []


async def test_query_1032_cancels_the_payment(settings, rematch_calls):
    buyer, order, payment = due_pair()
    db = FakeDB(
        results=[latest(payment), FakeResult(rows=[(payment.id, order.id)])],
        scalars=[PaymentStatus.INITIATED, None, order, payment],
    )
    await payment_service.maybe_reconcile_payment(
        db.factory, order, buyer, FakeDaraja(query=query_result("cancelled", 1032))
    )
    assert payment.status == PaymentStatus.CANCELLED
    assert order.status == OrderStatus.PENDING_PAYMENT


async def test_a_timed_out_payment_inside_the_window_is_reconciled(settings, rematch_calls):
    buyer, order, payment = due_pair(status=PaymentStatus.TIMEOUT, age=timedelta(minutes=6))
    order.status = OrderStatus.EXPIRED
    db = FakeDB(
        results=[latest(payment), FakeResult(rows=[(payment.id, order.id)])],
        scalars=[PaymentStatus.TIMEOUT, None, order, payment],
    )
    await payment_service.maybe_reconcile_payment(
        db.factory, order, buyer, FakeDaraja(query=query_result("success", 0))
    )
    assert payment.status == PaymentStatus.COMPLETED
    assert order.status == OrderStatus.NEEDS_RECONCILIATION


async def test_a_second_query_within_20_seconds_is_skipped(settings, rematch_calls):
    buyer, order, payment = due_pair()
    db = FakeDB(results=[latest(payment)], scalars=[PaymentStatus.INITIATED, timedelta(seconds=5)])
    daraja = FakeDaraja(query=query_result("success", 0))
    await payment_service.maybe_reconcile_payment(db.factory, order, buyer, daraja)
    assert daraja.queries == []
    assert payment.status == PaymentStatus.INITIATED


@pytest.mark.parametrize(
    ("status", "age"),
    [
        (PaymentStatus.INITIATED, timedelta(seconds=30)),
        (PaymentStatus.TIMEOUT, timedelta(minutes=16)),
        (PaymentStatus.COMPLETED, timedelta(minutes=1)),
    ],
)
async def test_payments_that_are_not_due_are_not_queried(settings, rematch_calls, status, age):
    buyer, order, payment = due_pair(status=status, age=age)
    db = FakeDB(results=[latest(payment)])
    daraja = FakeDaraja(query=query_result("success", 0))
    await payment_service.maybe_reconcile_payment(db.factory, order, buyer, daraja)
    assert daraja.queries == [] and rematch_calls == []


async def test_a_stale_initiating_payment_is_failed_by_reconciliation(settings):
    buyer, order, payment = due_pair(status=PaymentStatus.INITIATING, age=timedelta(seconds=61))
    payment.checkout_request_id = None
    db = FakeDB(results=[latest(payment)], scalars=[order, payment, NOW])
    daraja = FakeDaraja()
    await payment_service.maybe_reconcile_payment(db.factory, order, buyer, daraja)
    assert (payment.status, payment.result_desc) == (PaymentStatus.FAILED, "request_not_sent")
    assert daraja.queries == []
    assert lock_index(db.statements, "orders") < lock_index(db.statements, "payments")


async def test_admin_viewer_never_reconciles(settings):
    def exploding_factory():
        raise AssertionError("an admin view must not reconcile")

    order = make_order(uuid4())
    admin = make_buyer(role=UserRole.ADMIN)
    daraja = FakeDaraja()
    await payment_service.maybe_reconcile_payment(exploding_factory, order, admin, daraja)
    assert daraja.queries == []


async def test_reconciliation_errors_never_raise(settings):
    def broken_factory():
        raise RuntimeError("database unavailable")

    buyer, order, _ = due_pair()
    assert await payment_service.maybe_reconcile_payment(broken_factory, order, buyer, FakeDaraja()) is None


async def test_a_daraja_failure_never_fails_the_order_view(api_client, application, settings, rematch_calls):
    buyer, order, payment = due_pair()
    db = FakeDB(
        scalars=[order, PaymentStatus.INITIATED, None],
        results=[latest(payment), FakeResult(mapping_rows=[view_row(order)])],
    )
    daraja = FakeDaraja(query_error=DarajaRequestError("Daraja STK query failed (HTTP 503)"))
    install(application, db, buyer, daraja)
    response = await api_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200, response.text
    assert daraja.queries == [CHECKOUT_ID]
    assert [event.event_type for event in db.events] == ["status_query", "error"]


async def test_an_admin_order_view_never_queries_daraja(api_client, application, settings):
    order = make_order(uuid4())
    db = FakeDB(scalars=[order], results=[FakeResult(mapping_rows=[view_row(order)])])
    daraja = FakeDaraja(query=query_result("success", 0))
    install(application, db, make_buyer(role=UserRole.ADMIN), daraja)
    response = await api_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200, response.text
    assert daraja.queries == [] and db.events == []


# Event logging -------------------------------------------------------------------------------


async def test_log_event_redacts_and_caps_the_payload():
    db = FakeDB()
    payload = {"blob": "x" * 20000, "Password": "c2VjcmV0", "PhoneNumber": BUYER_PHONE}
    event_id = await payment_service.log_event(db.factory, "error", payload)
    stored = db.events[0]
    assert stored.id == event_id
    text = json.dumps(stored.payload)
    assert stored.payload["truncated"] is True
    assert len(text.encode()) <= payment_service.EVENT_PAYLOAD_MAX_BYTES
    assert "c2VjcmV0" not in text and BUYER_PHONE not in text


async def test_log_event_never_raises():
    def broken_factory():
        raise RuntimeError("database unavailable")

    assert await payment_service.log_event(broken_factory, "error", {"reason": "x"}) is None


# Secrets end to end --------------------------------------------------------------------------


async def test_secrets_never_reach_logs_events_responses_or_errors(api_client, application, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    settings = use_settings(monkeypatch, payment_settings())
    access_token = "AT-recognisable-access-token-77aa"
    flow_checkout = "ws_CO_flow_0001"

    def daraja_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": access_token, "expires_in": "3599"})
        if request.url.path == STK_PUSH_PATH and json.loads(request.content)["AccountReference"] == "FAIL0001":
            return httpx.Response(401, json={"errorMessage": "Invalid Access Token"})
        return httpx.Response(
            200,
            json={
                "MerchantRequestID": "m-1",
                "CheckoutRequestID": flow_checkout,
                "ResponseCode": "0",
                "ResponseDescription": "Success",
            },
        )

    client = DarajaClient(settings, transport=httpx.MockTransport(daraja_handler))
    assert SANDBOX_BASE_URL in str(client._http.base_url)
    buyer = make_buyer()
    order = make_order(buyer.id)
    failing = make_order(buyer.id, reference="FAIL0001")
    inserted = lambda db: db.payments[-1]  # noqa: E731
    db = FakeDB(
        scalars=[
            # successful pay: phase 1, phase 3 locks, rematch lookup
            None,
            order,
            NOW,
            0,
            0,
            order,
            inserted,
            lambda db: db.payments[-1].id,
            # callback: payment lookup, then the order and payment locks
            lambda db: db.payments[0].id,
            order,
            lambda db: db.payments[0],
            # failing pay: phase 1 and phase 3 locks
            None,
            failing,
            NOW,
            0,
            0,
            failing,
            inserted,
        ],
        lists=[[], []],
        results=[
            FakeResult(),  # rematch after the successful pay: no early callbacks to attach
            FakeResult(mapping_rows=[view_row(order)]),
            FakeResult(),
            lambda db: FakeResult(rows=[(db.payments[0].id, order.id)]),
        ],
    )
    install(application, db, buyer, client)

    paid = await post_pay(api_client, order.id)
    callback = await api_client.post(callback_url(), json=callback_body(flow_checkout, amount=114))
    failed = await post_pay(api_client, failing.id, headers={"Idempotency-Key": "pay-key-0002"})
    await client.aclose()

    assert paid.status_code == 202, paid.text
    assert callback.json() == ACK
    assert failed.status_code == 502
    assert db.payments[0].status == PaymentStatus.COMPLETED
    assert db.payments[1].status == PaymentStatus.FAILED
    haystacks = {
        "logs": caplog.text,
        "events": json.dumps([event.payload for event in db.events]),
        "responses": paid.text + callback.text + failed.text,
    }
    for name, haystack in haystacks.items():
        for secret in (*SECRETS, access_token):
            assert secret not in haystack, f"{secret!r} leaked into {name}"
    assert BUYER_PHONE not in haystacks["events"]
    assert "Password" not in haystacks["events"]


# Source checks -------------------------------------------------------------------------------

PAYMENT_FILES = [
    "integrations/daraja.py",
    "services/payment_service.py",
    "routers/payments.py",
    "routers/orders.py",
]
CHANGED_FILES = [
    *PAYMENT_FILES,
    "services/order_service.py",
    "schemas/order.py",
    "core/config.py",
    "main.py",
    "db.py",
    "utils/phone.py",
]


@pytest.mark.parametrize("name", PAYMENT_FILES)
def test_payment_code_never_uses_float(name):
    source = (APP_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"\bfloat\b", source) is None


@pytest.mark.parametrize("name", CHANGED_FILES)
def test_changed_files_import_no_ml_libraries(name):
    source = (APP_DIR / name).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines() if line.strip().startswith(("import ", "from "))]
    for line in imports:
        for banned in ("sklearn", "xgboost", "shap"):
            assert banned not in line, f"{name}: {line}"


def test_no_production_daraja_url_or_option_in_app():
    for path in APP_DIR.rglob("*.py"):
        assert "api.safaricom.co.ke" not in path.read_text(encoding="utf-8"), path
    for name in PAYMENT_FILES:
        source = (APP_DIR / name).read_text(encoding="utf-8")
        assert '"production"' not in source and "'production'" not in source, name


def test_payment_code_never_touches_listings():
    source = (APP_DIR / "services/payment_service.py").read_text(encoding="utf-8")
    assert "ProduceListing" not in source and "produce_listings" not in source


def test_pay_route_is_registered_for_post_only(application):
    paths = application.openapi()["paths"]
    assert paths["/api/v1/orders/{order_id}/pay"].keys() == {"post"}
