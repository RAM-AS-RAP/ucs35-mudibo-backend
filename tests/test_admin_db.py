"""Database-backed admin tests against the real triggers, constraints and the admin_action migration.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable database (including
migration 20261015000100_admin_resolution_event.sql). Every test runs inside the rollback-only
``db_session`` fixture; the payment code's own short transactions use sessions on the same connection.
"""

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.models.enums import CropType, ListingStatus, OrderStatus, PaymentStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment, PaymentEvent
from app.models.user import User
from app.schemas.admin import ResolveAction, ResolveOrderRequest
from app.schemas.common import Pagination
from app.schemas.listing import ListingBrowseFilters
from app.services import (
    admin_listing_service,
    admin_order_service,
    admin_user_service,
    listing_service,
    payment_service,
)

pytestmark = pytest.mark.db


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


def factory_for(db_session: AsyncSession):
    def factory() -> AsyncSession:
        return AsyncSession(bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")

    return factory


async def make_user(session: AsyncSession, role: UserRole, **values) -> User:
    user = User(
        name=f"Test {role.value}",
        phone_number=f"2547{secrets.randbelow(10**8):08d}",
        password_hash="not-a-real-hash",
        role=role,
        **values,
    )
    session.add(user)
    await session.commit()
    return user


async def super_admin(session: AsyncSession) -> User:
    existing = await session.scalar(select(User).where(User.is_super_admin.is_(True)))
    return existing or await make_user(session, UserRole.ADMIN, is_super_admin=True)


async def make_listing(session: AsyncSession, farmer: User | None = None, quantity: str = "10.00") -> ProduceListing:
    farmer = farmer or await make_user(session, UserRole.FARMER)
    listing = ProduceListing(
        farmer_id=farmer.id,
        crop=CropType.MAIZE,
        market="Nairobi",
        quantity_available=Decimal(quantity),
        price_per_unit=Decimal("45.50"),
    )
    session.add(listing)
    await session.commit()
    return listing


async def make_order(session: AsyncSession, listing: ProduceListing, buyer: User, expires_in=timedelta(minutes=5)):
    order = Order(
        reference=uuid4().hex[:8].upper(),
        listing_id=listing.id,
        buyer_id=buyer.id,
        buyer_role=UserRole.BUYER,
        quantity=Decimal("2.00"),
        unit_price=Decimal("45.50"),
        total_amount=91,
        status=OrderStatus.PENDING_PAYMENT,
        expires_at=func.now() + expires_in,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def reload(session: AsyncSession, model, row_id):
    return await session.scalar(select(model).where(model.id == row_id).execution_options(populate_existing=True))


async def test_the_migration_accepts_admin_action_events(db_session: AsyncSession) -> None:
    db_session.add(PaymentEvent(event_type="admin_action", payload={"action": "check"}))
    await db_session.flush()
    db_session.add(PaymentEvent(event_type="not_a_real_type", payload={}))
    with pytest.raises(DBAPIError):
        await db_session.flush()
    await db_session.rollback()


async def test_disable_satisfies_the_check_and_the_super_admin_trigger_refuses(db_session: AsyncSession) -> None:
    admin = await super_admin(db_session)
    farmer = await make_user(db_session, UserRole.FARMER)

    out = await admin_user_service.set_user_disabled(db_session, admin, farmer.id, True)
    assert out.is_disabled is True and out.disabled_at is not None
    stored = await reload(db_session, User, farmer.id)
    assert (stored.is_disabled, stored.disabled_by) == (True, admin.id)

    out = await admin_user_service.set_user_disabled(db_session, admin, farmer.id, False)
    assert (out.is_disabled, out.disabled_at) == (False, None)

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("update public.users set is_disabled = true, disabled_at = now() where id = :id"), {"id": admin.id}
        )
    await db_session.rollback()


async def test_removal_cancels_a_pending_order_and_restores_stock(db_session: AsyncSession) -> None:
    admin = await super_admin(db_session)
    buyer = await make_user(db_session, UserRole.BUYER)
    listing = await make_listing(db_session, quantity="8.00")
    order = await make_order(db_session, listing, buyer)

    await admin_listing_service.remove_listing(db_session, admin, listing.id)

    assert (await reload(db_session, Order, order.id)).status == OrderStatus.CANCELLED
    stored = await reload(db_session, ProduceListing, listing.id)
    assert stored.status == ListingStatus.REMOVED
    assert stored.quantity_available == Decimal("10.00")
    assert stored.removed_by == admin.id and stored.removed_at is not None


async def test_resolve_end_to_end_with_real_triggers(db_session: AsyncSession) -> None:
    factory = factory_for(db_session)
    admin = await super_admin(db_session)
    buyer = await make_user(db_session, UserRole.BUYER)
    listing = await make_listing(db_session, quantity="8.00")
    order = await make_order(db_session, listing, buyer)
    checkout_request_id = f"ws_CO_{uuid4().hex}"
    db_session.add(
        Payment(
            order_id=order.id,
            idempotency_key=f"key-{uuid4()}",
            phone_number=buyer.phone_number,
            stk_phone_number=buyer.phone_number,
            amount=order.total_amount,
            status=PaymentStatus.INITIATED,
            checkout_request_id=checkout_request_id,
        )
    )
    await db_session.commit()

    result = await payment_service.apply_payment_result(
        factory, checkout_request_id, "success", result_code=0, result_desc="ok", amount=1, source="callback"
    )
    assert result == "mismatch"
    assert (await reload(db_session, Order, order.id)).status == OrderStatus.NEEDS_RECONCILIATION
    await db_session.commit()

    detail = await admin_order_service.get_order(db_session, order.id)
    assert detail.reconciliation is not None
    assert (detail.reconciliation.reason, detail.reconciliation.order_status_before) == (
        "amount_mismatch",
        "pending_payment",
    )

    resolved = await admin_order_service.resolve_order(
        db_session,
        admin,
        order.id,
        ResolveOrderRequest(action=ResolveAction.CONFIRM_SALE, note="Buyer showed the M-Pesa message"),
    )
    assert resolved.status == "paid"
    assert resolved.resolution is not None and resolved.resolution.action == "confirm_sale"
    assert (await reload(db_session, ProduceListing, listing.id)).quantity_available == Decimal("8.00")
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(text("update public.orders set status = 'cancelled' where id = :id"), {"id": order.id})
    await db_session.rollback()


async def test_a_disabled_farmers_listing_is_hidden_and_returns_after_enabling(db_session: AsyncSession) -> None:
    admin = await super_admin(db_session)
    farmer = await make_user(db_session, UserRole.FARMER)
    listing = await make_listing(db_session, farmer)

    async def browse_ids() -> set:
        page = await listing_service.browse_listings(
            db_session, ListingBrowseFilters(), Pagination(limit=100, offset=0)
        )
        return {item.id for item in page.items}

    await admin_user_service.set_user_disabled(db_session, admin, farmer.id, True)
    assert listing.id not in await browse_ids()
    await admin_user_service.set_user_disabled(db_session, admin, farmer.id, False)
    assert listing.id in await browse_ids()
