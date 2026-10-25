"""Logging never contains secrets, codes, tokens, bearer values or full phone numbers.

The redaction and request context filters are attached to pytest's capture handler exactly as
``configure_logging`` attaches them to every real handler, then a login, a payment (STK push and
callback) and an invite flow run through the API at DEBUG level.
"""

import json
import logging
import sys

import pytest
from auth_fakes import AuthStore
from invite_fakes import FakeMailer, RecordingDB, configure_codes, inserted_invite, install, person
from payment_fakes import (
    BUYER_PHONE,
    CALLBACK_TOKEN,
    NOW,
    SECRETS,
    FakeDaraja,
    FakeDB,
    FakeResult,
    callback_body,
    make_buyer,
    make_order,
    payment_settings,
    use_settings,
    view_row,
)

from app.core.codes import format_code
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.logging_config import JsonFormatter, RedactionFilter, install_filters, redact_text
from app.db import get_db, get_session_factory
from app.integrations import email as email_integration
from app.integrations.daraja import get_daraja_client
from app.models.enums import UserRole
from app.services import auth_service, payment_service

LOGIN_PASSWORD = "Login-Unique-Passw0rd!"
LOGIN_EMAIL = "login.farmer@example.com"
LOGIN_PHONE = "254798765432"


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.DEBUG)
    install_filters(caplog.handler)
    return caplog


def all_log_text(caplog) -> str:
    formatter = logging.Formatter("%(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records) + "\n" + caplog.text


# Unit checks of the redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "hidden", "kept"),
    [
        ("payer 254712345678 paid", "254712345678", "254*******678"),
        ("payer 0712345678 paid", "0712345678", "254*******678"),
        ("payer +254112345678 paid", "+254112345678", "254*******678"),
        ("password=Hunter2-secret!", "Hunter2-secret!", "password=***"),
        ('{"new_password": "Abc-123-xyz!"}', "Abc-123-xyz!", '"new_password": "***'),
        ("Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi", "Authorization: ***"),
        ("sent Bearer eyJhbGciOi.abc.def upstream", "eyJhbGciOi", "Bearer ***"),
        ("access_token=tok_123456 refresh", "tok_123456", "access_token=***"),
        ("api-key: k-998877", "k-998877", "api-key: ***"),
        ("client_secret='s3cr3t-value'", "s3cr3t-value", "client_secret='***"),
        ("passkey=bfb279f9aa9bdbcf", "bfb279f9aa9bdbcf", "passkey=***"),
        ("code=K7QX-2MPA accepted", "K7QX-2MPA", "code=***"),
        ("POST /api/v1/payments/callback/CBT_secret_token_123 200", "CBT_secret_token_123", "/callback/***"),
    ],
)
def test_redact_text_masks_secrets_and_phone_numbers(raw, hidden, kept) -> None:
    redacted = redact_text(raw)
    assert hidden not in redacted
    assert kept in redacted


def test_redaction_keeps_ordinary_values() -> None:
    text = "status_code=200 result_code=1032 order=AB12CD34 amount=114 took 12.5ms id=6f1c0e8a"
    assert redact_text(text) == text


def test_the_filter_masks_arguments_and_tracebacks() -> None:
    try:
        raise ValueError("failed for 254712345678 with password=abc12345")
    except ValueError:
        record = logging.LogRecord(
            "app.test", logging.ERROR, __file__, 1, "user %s token=%s", ("254700000123", "tok-abc"), None
        )
        record.exc_info = sys.exc_info()
    assert RedactionFilter().filter(record)
    assert record.getMessage() == "user 254*******123 token=***"
    assert "254712345678" not in record.exc_text and "abc12345" not in record.exc_text
    assert "254*******678" in record.exc_text


def test_json_lines_have_the_documented_fields() -> None:
    record = logging.LogRecord("app.access", logging.INFO, __file__, 1, "GET /api/v1/health 200", None, None)
    record.request_id, record.method, record.path, record.status, record.duration_ms = (
        "req-1",
        "GET",
        "/api/v1/health",
        200,
        1.5,
    )
    line = json.loads(JsonFormatter().format(record))
    assert set(line) == {"time", "level", "logger", "message", "request_id", "method", "path", "status", "duration_ms"}
    assert line["request_id"] == "req-1" and line["status"] == 200


# Flows -----------------------------------------------------------------------------------------


async def test_login_flow_logs_no_password_code_token_or_email(api_client, application, monkeypatch, captured) -> None:
    configure_codes(monkeypatch, get_settings())
    store = AuthStore().install(application)
    mailer = FakeMailer(store)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    user = store.add_user(UserRole.FARMER, email=LOGIN_EMAIL, password=LOGIN_PASSWORD)
    auth_service.get_login_throttle().reset()
    started = await api_client.post("/api/v1/auth/login", json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD})
    assert started.status_code == 200, started.text
    challenge = started.json()["challenge_token"]
    code = mailer.code()
    verified = await api_client.post("/api/v1/auth/login/verify", json={"challenge_token": challenge, "code": code})
    assert verified.status_code == 200, verified.text
    token = verified.json()["access_token"]
    me = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    wrong = await api_client.post("/api/v1/auth/login", json={"email": user.email, "password": "Wrong-Pass-1!"})
    assert wrong.status_code == 401
    logging.getLogger("app.test").info(
        "login check for %s with %s, %s and code=%s", user.email, f"Bearer {token}", LOGIN_PHONE, format_code(code)
    )

    text = all_log_text(captured)
    assert "GET /api/v1/auth/me 200" in text  # the access log line is there
    for secret in (
        LOGIN_PASSWORD,
        "Wrong-Pass-1!",
        token,
        challenge,
        code,
        format_code(code),
        LOGIN_EMAIL,
        LOGIN_PHONE,
    ):
        assert secret not in text
    assert "l***@e***.com" in text and "254*******432" in text


def test_redaction_masks_email_addresses() -> None:
    redacted = redact_text("sent to grace.wanjiru+demo@example.co.ke and amina@example.com")
    assert "grace.wanjiru" not in redacted and "amina@example.com" not in redacted
    assert "g***@e***.ke" in redacted and "a***@e***.com" in redacted


async def test_payment_flow_logs_no_daraja_secret_token_or_phone(
    api_client, application, monkeypatch, captured
) -> None:
    use_settings(monkeypatch, payment_settings())

    async def no_rematch(session_factory, checkout_request_id):
        del session_factory, checkout_request_id

    monkeypatch.setattr(payment_service, "rematch_orphan_events", no_rematch)
    buyer = make_buyer()
    order = make_order(buyer.id)
    db = FakeDB(
        # replay, order, clock, attempts, per number, per buyer; phase 3 locks
        scalars=[None, order, NOW, 0, 0, 0, order, lambda db: db.payments[-1]],
        lists=[[]],
        # distinct numbers used today, then the order view
        results=[FakeResult(), FakeResult(mapping_rows=[view_row(order)])],
    )

    async def request_session():
        yield db.session()

    application.dependency_overrides[get_current_user] = lambda: buyer
    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_session_factory] = lambda: db.factory
    application.dependency_overrides[get_daraja_client] = lambda: FakeDaraja(db)
    paid = await api_client.post(
        f"/api/v1/orders/{order.id}/pay",
        headers={"Idempotency-Key": "log-check-0001"},
        json={"phone_number": BUYER_PHONE},
    )
    assert paid.status_code == 202, paid.text

    callback_db = FakeDB(scalars=[None])
    application.dependency_overrides[get_session_factory] = lambda: callback_db.factory
    callback = await api_client.post(
        f"/api/v1/payments/callback/{CALLBACK_TOKEN}",
        content=json.dumps(callback_body()).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert callback.status_code == 200

    text = all_log_text(captured)
    assert "/api/v1/payments/callback/*** 200" in text
    for secret in (*SECRETS, BUYER_PHONE):
        assert secret not in text


async def test_invite_flow_logs_no_code(api_client, application, monkeypatch, captured) -> None:
    configure_codes(monkeypatch, get_settings())
    db = install(
        application,
        person(super_admin=True),
        RecordingDB(scalars=[False, 0], results=[FakeResult(), FakeResult(), inserted_invite]),
    )
    mailer = FakeMailer(db)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    response = await api_client.post(
        "/api/v1/admin/invites",
        json={"name": "Grace Admin", "email": "grace@example.com"},
    )
    assert response.status_code == 201, response.text
    code = mailer.code()
    logging.getLogger("app.test").info("invite code=%s for %s", format_code(code), "grace@example.com")

    text = all_log_text(captured)
    for secret in (code, format_code(code), "grace@example.com"):
        assert secret not in text
