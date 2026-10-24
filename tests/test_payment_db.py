"""Database-backed payment tests against the real triggers, indexes and expiry job.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable database. Every test
except the concurrency test runs inside the rollback-only ``db_session`` fixture; the payment code's own
short transactions use sessions on the same connection, so they are rolled back too. Daraja is
replaced by a fake client.

The concurrency test needs two real connections, so it commits: it leaves disabled ``ZZ_TEST_`` users
(users cannot be deleted; disabling hides their listings), their orders (expired later by the expiry
job) and one payment row behind.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.db import get_engine, get_sessionmaker
from app.integrations.daraja import StkPushResult
from app.models.enums import CropType, OrderStatus, PaymentStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services import order_service, payment_service

pytestmark = pytest.mark.db


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


@pytest.fixture
def payment_settings(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings().model_copy(
        update={
            "mpesa_consumer_key": "key",
            "mpesa_consumer_secret": "secret",
            "mpesa_passkey": "passkey",
            "mpesa_callback_base_url": "https://payments.example.test",
            "mpesa_callback_token": "t" * 32,
            "mpesa_stk_target": "registered",
        }
    )
    monkeypatch.setattr(payment_service, "get_settings", lambda: settings)
    return settings


def factory_for(db_session: AsyncSession):
    def factory() -> AsyncSession:
        return AsyncSession(bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")

    return factory


class AcceptingDaraja:
    def __init__(self) -> None:
        self.checkout_request_id = f"ws_CO_{uuid4().hex}"

    async def stk_push(self, phone: str, amount: int, account_reference: str, callback_url: str) -> StkPushResult:
        del phone, amount, account_reference, callback_url
        return StkPushResult(True, f"mr-{uuid4().hex[:8]}", self.checkout_request_id, "Success", {}, {})


async def make_user(session: AsyncSession, role: UserRole, name: str | None = None) -> User:
    user = User(
        name=name or f"Test {role.value}",
        phone_number=f"2547{secrets.randbelow(10**8):08d}",
        password_hash="not-a-real-hash",
        role=role,
    )
    session.add(user)
    await session.commit()
    return user


async def make_listing(session: AsyncSession, farmer_name: str | None = None) -> ProduceListing:
    farmer = await make_user(session, UserRole.FARMER, farmer_name)
    listing = ProduceListing(
        farmer_id=farmer.id,
        crop=CropType.MAIZE,
        market="Nairobi",
        quantity_available=Decimal("10.00"),
        price_per_unit=Decimal("45.50"),
    )
    session.add(listing)
    await session.commit()
    return listing


async def make_order(
    session: AsyncSession,
    buyer: User,
    *,
    status: OrderStatus = OrderStatus.PENDING_PAYMENT,
    expires_in=None,
    farmer_name: str | None = None,
) -> Order:
    listing = await make_listing(session, farmer_name)
    order = Order(
        reference=uuid4().hex[:8].upper(),
        listing_id=listing.id,
        buyer_id=buyer.id,
        buyer_role=UserRole.BUYER,
        quantity=Decimal("1.00"),
        unit_price=Decimal("45.50"),
        total_amount=46,
        status=status,
        expires_at=func.now() + (expires_in if expires_in is not None else timedelta(minutes=5)),
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


def payment_for(order: Order, buyer: User, **overrides) -> Payment:
    values = {
        "order_id": order.id,
        "idempotency_key": f"key-{uuid4()}",
        "phone_number": buyer.phone_number,
        "stk_phone_number": buyer.phone_number,
        "amount": order.total_amount,
        "status": PaymentStatus.INITIATING,
    }
    values.update(overrides)
    return Payment(**values)


async def reload(session: AsyncSession, model, row_id: UUID):
    return await session.scalar(select(model).where(model.id == row_id).execution_options(populate_existing=True))


async def test_insert_trigger_rejects_an_amount_that_differs_from_the_order_total(db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order(db_session, buyer)
    db_session.add(payment_for(order, buyer, amount=order.total_amount + 1))
    with pytest.raises(DBAPIError):
        await db_session.flush()
    await db_session.rollback()


async def test_insert_trigger_rejects_an_order_that_is_not_pending(db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order(db_session, buyer, status=OrderStatus.CANCELLED)
    db_session.add(payment_for(order, buyer))
    with pytest.raises(DBAPIError):
        await db_session.flush()
    await db_session.rollback()


async def test_a_second_open_payment_violates_the_partial_unique_index(db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order(db_session, buyer)
    db_session.add(payment_for(order, buyer))
    await db_session.commit()
    db_session.add(payment_for(order, buyer, status=PaymentStatus.INITIATING))
    with pytest.raises(DBAPIError) as exc_info:
        await db_session.flush()
    assert "payments_one_open_per_order_idx" in str(exc_info.value)
    await db_session.rollback()


async def test_success_flow_end_to_end_with_the_real_triggers(db_session: AsyncSession, payment_settings) -> None:
    del payment_settings
    factory = factory_for(db_session)
    buyer = await make_user(db_session, UserRole.BUYER)
    listing = await make_listing(db_session)
    created = await order_service.create_order(db_session, buyer, listing.id, Decimal("2"))
    daraja = AcceptingDaraja()

    started = await payment_service.initiate_payment(db_session, factory, daraja, buyer, created.id, "db-flow-key-0001")

    assert started.stk_target_masked.endswith(buyer.phone_number[-3:])
    payment = await db_session.scalar(
        select(Payment).where(Payment.order_id == created.id).execution_options(populate_existing=True)
    )
    assert payment.status == PaymentStatus.INITIATED
    assert payment.checkout_request_id == daraja.checkout_request_id
    assert payment.amount == created.total_amount
    assert payment.idempotency_key == f"{buyer.id}:{created.id}:db-flow-key-0001"
    await db_session.commit()

    receipt = f"SBX{secrets.token_hex(4).upper()}"
    result = await payment_service.apply_payment_result(
        factory,
        daraja.checkout_request_id,
        "success",
        result_code=0,
        result_desc="The service request is processed successfully.",
        receipt=receipt,
        amount=created.total_amount,
        source="callback",
    )

    assert result == "applied"
    payment = await reload(db_session, Payment, payment.id)
    order = await reload(db_session, Order, created.id)
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.mpesa_receipt_number == receipt
    assert payment.callback_received_at is not None
    assert order.status == OrderStatus.PAID
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("update public.payments set result_desc = 'changed' where id = :id"), {"id": payment.id}
        )
    await db_session.rollback()

    again = await payment_service.apply_payment_result(
        factory,
        daraja.checkout_request_id,
        "success",
        result_code=0,
        result_desc="again",
        receipt=receipt,
        amount=created.total_amount,
        source="callback",
    )
    assert again == "duplicate"


async def test_expiry_job_times_out_the_payment_and_a_late_success_needs_reconciliation(
    db_session: AsyncSession,
) -> None:
    factory = factory_for(db_session)
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order(db_session, buyer, expires_in=timedelta(minutes=-1))
    checkout_request_id = f"ws_CO_{uuid4().hex}"
    payment = payment_for(order, buyer, status=PaymentStatus.INITIATED, checkout_request_id=checkout_request_id)
    db_session.add(payment)
    await db_session.commit()

    await db_session.execute(text("select public.expire_stale_orders()"))
    await db_session.commit()

    assert (await reload(db_session, Payment, payment.id)).status == PaymentStatus.TIMEOUT
    assert (await reload(db_session, Order, order.id)).status == OrderStatus.EXPIRED
    await db_session.commit()

    result = await payment_service.apply_payment_result(
        factory,
        checkout_request_id,
        "success",
        result_code=0,
        result_desc="The service request is processed successfully.",
        receipt=f"SBX{secrets.token_hex(4).upper()}",
        amount=order.total_amount,
        source="callback",
    )

    assert result == "applied"
    assert (await reload(db_session, Payment, payment.id)).status == PaymentStatus.COMPLETED
    assert (await reload(db_session, Order, order.id)).status == OrderStatus.NEEDS_RECONCILIATION


# The number entered when paying --------------------------------------------------------------


def limit_settings(monkeypatch: pytest.MonkeyPatch, **overrides):
    settings = get_settings().model_copy(
        update={
            "mpesa_consumer_key": "key",
            "mpesa_consumer_secret": "secret",
            "mpesa_passkey": "passkey",
            "mpesa_callback_base_url": "https://payments.example.test",
            "mpesa_callback_token": "t" * 32,
            "mpesa_stk_target": "entered",
            **overrides,
        }
    )
    monkeypatch.setattr(payment_service, "get_settings", lambda: settings)
    return settings


async def closed_payment(session: AsyncSession, order: Order, buyer: User, phone: str, age: timedelta) -> None:
    """A failed attempt created ``age`` before the transaction's clock, so it no longer blocks the order."""
    payment = payment_for(order, buyer, phone_number=phone, stk_phone_number=phone, created_at=func.now() - age)
    session.add(payment)
    await session.commit()
    payment.status = PaymentStatus.FAILED
    payment.result_desc = "request_rejected"
    await session.commit()


async def test_the_entered_number_is_stored_and_never_saved_to_the_user(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit_settings(monkeypatch)
    buyer = await make_user(db_session, UserRole.BUYER)
    profile_phone = buyer.phone_number
    order = await make_order(db_session, buyer)

    await payment_service.initiate_payment(
        db_session, factory_for(db_session), AcceptingDaraja(), buyer, order.id, "entered-key-0001", "0711 222 333"
    )

    payment = await db_session.scalar(
        select(Payment).where(Payment.order_id == order.id).execution_options(populate_existing=True)
    )
    assert (payment.phone_number, payment.stk_phone_number) == ("254711222333", "254711222333")
    assert (await reload(db_session, User, buyer.id)).phone_number == profile_phone


async def test_test_msisdn_mode_lets_two_buyers_with_different_numbers_pay(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old per phone rule counted stk_phone_number, which is the same test number for every payment here.
    limit_settings(monkeypatch, mpesa_stk_target="test_msisdn", mpesa_phone_attempts_per_hour=1)
    started = []
    for number in (f"07{secrets.randbelow(10**8):08d}", f"01{secrets.randbelow(10**8):08d}"):
        buyer = await make_user(db_session, UserRole.BUYER)
        order = await make_order(db_session, buyer)
        started.append(
            await payment_service.initiate_payment(
                db_session, factory_for(db_session), AcceptingDaraja(), buyer, order.id, f"k-{uuid4().hex}", number
            )
        )
    assert [start.stk_target_masked for start in started] == ["254*******149", "254*******149"]


async def test_buyer_and_distinct_number_limits_use_the_database_clock(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit_settings(monkeypatch, mpesa_buyer_attempts_per_hour=2, mpesa_buyer_distinct_phones_per_day=3)
    factory = factory_for(db_session)
    buyer = await make_user(db_session, UserRole.BUYER)
    numbers = [f"2547{secrets.randbelow(10**8):08d}" for _ in range(4)]
    # Three numbers used today, all more than an hour ago; the oldest leaves the 24 hour window in 1 hour.
    for phone, age in zip(numbers[:3], (timedelta(hours=23), timedelta(hours=5), timedelta(hours=2)), strict=True):
        await closed_payment(db_session, await make_order(db_session, buyer), buyer, phone, age)

    target = await make_order(db_session, buyer)
    with pytest.raises(AppError) as exc_info:
        await payment_service.initiate_payment(
            db_session, factory, AcceptingDaraja(), buyer, target.id, "distinct-key-0001", numbers[3]
        )
    assert (exc_info.value.code, exc_info.value.retry_after) == ("too_many_phone_numbers", 3600)
    await db_session.rollback()

    # Re-using one of the numbers is allowed at the distinct limit.
    await payment_service.initiate_payment(
        db_session, factory, AcceptingDaraja(), buyer, target.id, "distinct-key-0002", numbers[0]
    )
    await db_session.commit()

    # Two attempts in the last hour (this one and one 40 minutes old) reach the hourly buyer limit.
    await closed_payment(db_session, await make_order(db_session, buyer), buyer, numbers[0], timedelta(minutes=40))
    another = await make_order(db_session, buyer)
    with pytest.raises(AppError) as exc_info:
        await payment_service.initiate_payment(
            db_session, factory, AcceptingDaraja(), buyer, another.id, "buyer-key-0001", numbers[0]
        )
    assert exc_info.value.code == "too_many_buyer_payment_attempts"
    # The oldest payment in the window is 40 minutes old on the transaction's clock.
    assert exc_info.value.retry_after == 20 * 60
    await db_session.rollback()


async def test_concurrent_payments_for_two_orders_of_one_buyer_respect_the_buyer_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit_settings(monkeypatch, mpesa_buyer_attempts_per_hour=1)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        buyer = await make_user(session, UserRole.BUYER, "ZZ_TEST_ concurrent buyer")
        orders = [await make_order(session, buyer, farmer_name="ZZ_TEST_ concurrent farmer") for _ in range(2)]
        farmer_ids = [
            (await session.scalar(select(ProduceListing.farmer_id).where(ProduceListing.id == order.listing_id)))
            for order in orders
        ]

    async def attempt(order: Order, key: str) -> str:
        async with sessionmaker() as session:
            try:
                await payment_service.initiate_payment(
                    session, sessionmaker, AcceptingDaraja(), buyer, order.id, key, buyer.phone_number
                )
            except AppError as exc:
                return exc.code
            return "started"

    try:
        results = await asyncio.gather(
            attempt(orders[0], "concurrent-key-0001"), attempt(orders[1], "concurrent-key-0002")
        )
        assert sorted(results) == ["started", "too_many_buyer_payment_attempts"]
        async with sessionmaker() as session:
            count = await session.scalar(
                select(func.count(Payment.id)).where(Payment.order_id.in_([order.id for order in orders]))
            )
            assert count == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("update public.users set is_disabled = true, disabled_at = now() where id = any(:ids)"),
                {"ids": [buyer.id, *farmer_ids]},
            )
            await session.commit()
