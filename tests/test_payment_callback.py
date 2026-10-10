"""Callback tests for token validation, idempotency, and amount matching."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import get_db
from app.models.enums import OrderStatus, PaymentStatus
from app.routers import payments as payment_router
from app.services.payment_service import handle_callback


class FakeSession:
    def __init__(self, payment=None, rowcount=0):
        self.payment = payment
        self.rowcount = rowcount
        self.events = []
        self.scalar_calls = 0
        self.execute_calls = 0

    def add(self, item):
        self.events.append(item)

    async def commit(self):
        return None

    async def scalar(self, statement):
        del statement
        self.scalar_calls += 1
        if self.payment is None:
            return None
        if self.scalar_calls == 1:
            return self.payment
        return self.payment.order

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, statement):
        del statement
        self.execute_calls += 1
        return SimpleNamespace(rowcount=self.rowcount)


async def fake_db(session):
    yield session


def override_db(session: FakeSession):
    async def dependency():
        yield session

    return dependency


def callback(amount=250, result_code=0):
    return {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": result_code,
                "ResultDesc": "ok",
                "CallbackMetadata": {
                    "Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": "receipt-1"}]
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_wrong_callback_token_returns_404(api_client, application, monkeypatch) -> None:
    application.dependency_overrides[get_db] = override_db(FakeSession())
    monkeypatch.setattr(payment_router, "get_settings", lambda: SimpleNamespace(mpesa_callback_token="expected"))
    response = await api_client.post("/api/v1/payments/callback/wrong", json=callback())
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unknown_checkout_request_is_acknowledged(api_client, application, monkeypatch) -> None:
    session = FakeSession()
    application.dependency_overrides[get_db] = override_db(session)
    monkeypatch.setattr(payment_router, "get_settings", lambda: SimpleNamespace(mpesa_callback_token="expected"))
    response = await api_client.post("/api/v1/payments/callback/expected", json=callback())
    assert response.status_code == 200
    assert response.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert len(session.events) == 1


@pytest.mark.asyncio
async def test_duplicate_callback_does_not_change_order_state() -> None:
    order = SimpleNamespace(id=uuid4(), status=OrderStatus.PENDING_PAYMENT)
    payment = SimpleNamespace(id=uuid4(), order_id=order.id, amount=250, status=PaymentStatus.COMPLETED, order=order)
    session = FakeSession(payment=payment, rowcount=0)
    await handle_callback(session, callback())
    assert session.execute_calls == 1
    assert order.status == OrderStatus.PENDING_PAYMENT


@pytest.mark.asyncio
async def test_amount_mismatch_does_not_mark_order_paid() -> None:
    order = SimpleNamespace(id=uuid4(), status=OrderStatus.PENDING_PAYMENT)
    payment = SimpleNamespace(id=uuid4(), order_id=order.id, amount=250, status=PaymentStatus.INITIATED, order=order)
    session = FakeSession(payment=payment, rowcount=1)
    await handle_callback(session, callback(amount=249))
    assert session.execute_calls == 0
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert len(session.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Body": "not-an-object"},
        {"Body": {"stkCallback": "not-an-object"}},
        {"Body": {"stkCallback": {"CheckoutRequestID": {"nested": 1}, "CallbackMetadata": "not-an-object"}}},
        {"Body": {"stkCallback": {"CallbackMetadata": {"Item": "not-a-list"}}}},
    ],
)
async def test_malformed_callback_is_recorded_without_error(payload) -> None:
    session = FakeSession()
    await handle_callback(session, payload)
    assert len(session.events) == 1
    assert session.events[0].checkout_request_id is None
    assert session.scalar_calls == 0


@pytest.mark.asyncio
async def test_malformed_callback_body_is_acknowledged(api_client, application, monkeypatch) -> None:
    session = FakeSession()
    application.dependency_overrides[get_db] = override_db(session)
    monkeypatch.setattr(payment_router, "get_settings", lambda: SimpleNamespace(mpesa_callback_token="expected"))
    response = await api_client.post("/api/v1/payments/callback/expected", json={"Body": "not-an-object"})
    assert response.status_code == 200
    assert response.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
