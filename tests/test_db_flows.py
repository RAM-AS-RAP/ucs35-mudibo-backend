"""Database-backed route tests for orders, payments, and enum query validation.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a migrated, disposable database.
Each test runs inside the rollback-only ``db_session`` fixture, so nothing is left behind.
Requests go through the real authentication dependency; only ``get_db`` is redirected.
"""

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db import get_db, get_engine
from app.models.enums import CropType, ListingStatus, OrderStatus, PaymentStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services import payment_service

pytestmark = pytest.mark.db


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    """Close pooled connections so the next test's event loop opens its own."""
    yield
    await get_engine().dispose()


@pytest.fixture
def db_client(application, api_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    async def dependency() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db] = dependency
    return api_client


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


async def make_user(session: AsyncSession, role: UserRole) -> User:
    user = User(
        name=f"Test {role.value}",
        phone_number=f"2547{secrets.randbelow(10**8):08d}",
        password_hash="not-a-real-hash",
        role=role,
    )
    session.add(user)
    await session.commit()
    return user


async def make_listing(session: AsyncSession, quantity: str = "10.00") -> ProduceListing:
    farmer = await make_user(session, UserRole.FARMER)
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


async def make_order_with_open_payment(session: AsyncSession, buyer: User) -> Order:
    listing = await make_listing(session)
    order = Order(
        reference=uuid4().hex[:8].upper(),
        listing_id=listing.id,
        buyer_id=buyer.id,
        buyer_role=UserRole.BUYER,
        quantity=Decimal("1.00"),
        unit_price=Decimal("45.50"),
        total_amount=46,
        status=OrderStatus.PENDING_PAYMENT,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(order)
    await session.commit()
    session.add(
        Payment(
            order_id=order.id,
            idempotency_key=f"key-{uuid4()}",
            phone_number=buyer.phone_number,
            stk_phone_number=buyer.phone_number,
            amount=order.total_amount,
            status=PaymentStatus.INITIATED,
            checkout_request_id=f"ws_CO_{uuid4().hex}",
        )
    )
    await session.commit()
    return order


async def test_buyer_creates_order_and_stock_is_reserved(db_client: AsyncClient, db_session: AsyncSession) -> None:
    listing = await make_listing(db_session, quantity="10.00")
    buyer = await make_user(db_session, UserRole.BUYER)

    response = await db_client.post(
        "/api/v1/orders", json={"listing_id": str(listing.id), "quantity": "2.5"}, headers=auth(buyer)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["buyer_id"] == str(buyer.id)
    assert body["total_amount"] == 114
    assert body["status"] == "pending_payment"
    await db_session.refresh(listing)
    assert listing.quantity_available == Decimal("7.50")
    assert listing.status == ListingStatus.ACTIVE


async def test_ordering_all_stock_marks_listing_sold_out(db_client: AsyncClient, db_session: AsyncSession) -> None:
    listing = await make_listing(db_session, quantity="3.00")
    buyer = await make_user(db_session, UserRole.BUYER)

    response = await db_client.post(
        "/api/v1/orders", json={"listing_id": str(listing.id), "quantity": "3"}, headers=auth(buyer)
    )

    assert response.status_code == 201, response.text
    await db_session.refresh(listing)
    assert listing.quantity_available == Decimal("0.00")
    assert listing.status == ListingStatus.SOLD_OUT


async def test_order_above_stock_is_rejected_and_stock_unchanged(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    listing = await make_listing(db_session, quantity="1.00")
    buyer = await make_user(db_session, UserRole.BUYER)

    response = await db_client.post(
        "/api/v1/orders", json={"listing_id": str(listing.id), "quantity": "2"}, headers=auth(buyer)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stock_unavailable"
    await db_session.refresh(listing)
    assert listing.quantity_available == Decimal("1.00")


async def test_order_detail_includes_latest_payment(db_client: AsyncClient, db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order_with_open_payment(db_session, buyer)

    response = await db_client.get(f"/api/v1/orders/{order.id}", headers=auth(buyer))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order"]["id"] == str(order.id)
    assert body["latest_payment"]["order_id"] == str(order.id)
    assert body["latest_payment"]["status"] == "initiated"
    assert body["latest_payment"]["amount"] == 46


async def test_second_payment_while_one_is_open_returns_409(
    db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_daraja(*args: object, **kwargs: object) -> None:
        raise AssertionError("a rejected payment attempt must not contact Daraja")

    monkeypatch.setattr(payment_service, "DarajaClient", no_daraja)
    buyer = await make_user(db_session, UserRole.BUYER)
    order = await make_order_with_open_payment(db_session, buyer)

    response = await db_client.post(
        f"/api/v1/orders/{order.id}/pay", headers={**auth(buyer), "Idempotency-Key": f"key-{uuid4()}"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "payment_in_progress"


@pytest.mark.parametrize(
    ("role", "path"),
    [
        (UserRole.BUYER, "/api/v1/prices/current?crop=rice"),
        (UserRole.BUYER, "/api/v1/prices/trends?market=Nairobi&crop=rice"),
        (UserRole.FARMER, "/api/v1/forecasts?market=Nairobi&crop=rice&horizon=one_month"),
        (UserRole.FARMER, "/api/v1/forecasts?market=Nairobi&crop=maize&horizon=bogus"),
    ],
)
async def test_unknown_crop_or_horizon_returns_422(
    db_client: AsyncClient, db_session: AsyncSession, role: UserRole, path: str
) -> None:
    user = await make_user(db_session, role)

    response = await db_client.get(path, headers=auth(user))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_known_crop_and_horizon_still_reach_the_database(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    farmer = await make_user(db_session, UserRole.FARMER)

    prices = await db_client.get("/api/v1/prices/current?crop=maize", headers=auth(farmer))
    trends = await db_client.get("/api/v1/prices/trends?market=Nairobi&crop=beans", headers=auth(farmer))
    forecast = await db_client.get(
        "/api/v1/forecasts?market=Nairobi&crop=maize&horizon=two_month", headers=auth(farmer)
    )

    assert prices.status_code == 200
    assert trends.status_code == 200
    assert forecast.status_code in {200, 404}
