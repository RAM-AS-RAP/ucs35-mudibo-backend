"""Database tests for the farmer single-listing read, the buyer markets list and the shared visibility rule.

Skipped unless RUN_DB_TESTS=1 and DATABASE_URL points at a local migrated database. Each test runs in the
rollback-only ``db_session`` fixture. A local database may already hold seeded listings, so market counts
are compared as differences from a baseline taken before the test creates its rows.
"""

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from response_helpers import without_request_id
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import create_access_token, decode_access_token, hash_password
from app.db import get_db, get_engine, get_session_factory
from app.models.enums import CropType, ListingStatus, UserRole
from app.models.listing import ProduceListing
from app.models.user import User
from app.schemas.common import Pagination
from app.schemas.listing import ListingBrowseFilters
from app.services import auth_service, listing_service, order_service

pytestmark = pytest.mark.db

NOT_FOUND = {"error": {"code": "listing_not_found", "message": "Listing not found."}}
PASSWORD = "Correct-horse-9!"


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_after_test() -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


def session_factory_for(db_session: AsyncSession):
    def factory() -> AsyncSession:
        return AsyncSession(bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")

    return factory


@pytest.fixture
def db_client(application, api_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    async def dependency() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db] = dependency
    application.dependency_overrides[get_session_factory] = lambda: session_factory_for(db_session)
    return api_client


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


async def make_user(session: AsyncSession, role: UserRole, **values) -> User:
    user = User(
        name=f"Test {role.value}",
        phone_number=f"2547{secrets.randbelow(10**8):08d}",
        password_hash=values.pop("password_hash", "not-a-real-hash"),
        role=role,
        **values,
    )
    session.add(user)
    await session.commit()
    return user


async def make_listing(
    session: AsyncSession,
    farmer: User,
    *,
    market: str = "Nairobi",
    status: ListingStatus = ListingStatus.ACTIVE,
    quantity: str = "10.00",
) -> ProduceListing:
    listing = ProduceListing(
        farmer_id=farmer.id,
        crop=CropType.MAIZE,
        market=market,
        quantity_available=Decimal("0.00") if status == ListingStatus.SOLD_OUT else Decimal(quantity),
        price_per_unit=Decimal("45.50"),
        status=status,
        removed_at=datetime.now(UTC) if status == ListingStatus.REMOVED else None,
        removed_by=farmer.id if status == ListingStatus.REMOVED else None,
    )
    session.add(listing)
    await session.commit()
    return listing


async def disable(session: AsyncSession, user: User) -> None:
    user.is_disabled = True
    user.disabled_at = datetime.now(UTC)
    await session.commit()


async def market_counts(client: AsyncClient, buyer: User) -> dict[str, int]:
    response = await client.get("/api/v1/listings/markets", headers=auth(buyer))
    assert response.status_code == 200, response.text
    return {item["name"]: item["listing_count"] for item in response.json()}


def delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    changes = {name: after.get(name, 0) - before.get(name, 0) for name in set(after) | set(before)}
    return {name: change for name, change in changes.items() if change}


# is_super_admin from the database ------------------------------------------------------------


async def test_login_and_me_return_the_super_admin_flag_from_the_row(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    password_hash = hash_password(PASSWORD)
    super_admin = await db_session.scalar(select(User).where(User.is_super_admin.is_(True)))
    if super_admin is None:
        super_admin = await make_user(db_session, UserRole.ADMIN, is_super_admin=True)
    ordinary = await make_user(
        db_session, UserRole.ADMIN, email=f"admin-{uuid4().hex[:8]}@example.com", password_hash=password_hash
    )
    farmer = await make_user(db_session, UserRole.FARMER, password_hash=password_hash)
    buyer = await make_user(db_session, UserRole.BUYER, password_hash=password_hash)
    for user, expected in [(super_admin, True), (ordinary, False), (farmer, False), (buyer, False)]:
        me = await db_client.get("/api/v1/auth/me", headers=auth(user))
        assert me.status_code == 200, me.text
        assert me.json()["is_super_admin"] is expected
    auth_service.get_login_throttle().reset()
    login = await auth_service.login(db_session, ordinary.email, PASSWORD)
    assert login["is_super_admin"] is False
    assert "is_super_admin" not in decode_access_token(login["access_token"])


# GET /listings/mine/{listing_id} -------------------------------------------------------------


async def test_owner_reads_active_sold_out_and_removed_listings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    farmer = await make_user(db_session, UserRole.FARMER)
    for status in (ListingStatus.ACTIVE, ListingStatus.SOLD_OUT, ListingStatus.REMOVED):
        listing = await make_listing(db_session, farmer, status=status)
        response = await db_client.get(f"/api/v1/listings/mine/{listing.id}", headers=auth(farmer))
        assert response.status_code == 200, response.text
        assert (response.json()["id"], response.json()["status"]) == (str(listing.id), status.value)

    mine = await db_client.get("/api/v1/listings/mine", headers=auth(farmer))
    item = mine.json()["items"][0]
    single = await db_client.get(f"/api/v1/listings/mine/{item['id']}", headers=auth(farmer))
    assert single.json() == item


async def test_other_farmers_listing_and_unknown_id_are_identical_404s(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, UserRole.FARMER)
    other = await make_user(db_session, UserRole.FARMER)
    listing = await make_listing(db_session, owner)
    foreign = await db_client.get(f"/api/v1/listings/mine/{listing.id}", headers=auth(other))
    unknown = await db_client.get(f"/api/v1/listings/mine/{uuid4()}", headers=auth(other))
    assert foreign.status_code == unknown.status_code == 404
    assert without_request_id(foreign.json()) == without_request_id(unknown.json()) == NOT_FOUND


# GET /listings/markets -----------------------------------------------------------------------


async def test_markets_count_only_buyer_visible_listings(db_client: AsyncClient, db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    before = await market_counts(db_client, buyer)

    farmer = await make_user(db_session, UserRole.FARMER)
    disabled_farmer = await make_user(db_session, UserRole.FARMER)
    await make_listing(db_session, farmer, market="Mombasa")
    await make_listing(db_session, farmer, market="Mombasa")
    await make_listing(db_session, farmer, market="Nakuru")
    await make_listing(db_session, farmer, market="Mombasa", status=ListingStatus.SOLD_OUT)
    await make_listing(db_session, farmer, market="Mombasa", status=ListingStatus.REMOVED)
    await make_listing(db_session, farmer, market="Kisumu", status=ListingStatus.REMOVED)
    await make_listing(db_session, disabled_farmer, market="Mombasa")
    await make_listing(db_session, disabled_farmer, market="Kisumu")
    await disable(db_session, disabled_farmer)

    after = await market_counts(db_client, buyer)
    assert delta(after, before) == {"Mombasa": 2, "Nakuru": 1}
    names = list(after)
    assert names == sorted(names)
    assert all(count > 0 for count in after.values())


async def test_zero_stock_listing_is_not_counted(db_client: AsyncClient, db_session: AsyncSession) -> None:
    # An active listing with zero stock is impossible by constraint; zero stock means sold out.
    buyer = await make_user(db_session, UserRole.BUYER)
    farmer = await make_user(db_session, UserRole.FARMER)
    before = await market_counts(db_client, buyer)
    await make_listing(db_session, farmer, market="Nakuru", status=ListingStatus.SOLD_OUT)
    assert delta(await market_counts(db_client, buyer), before) == {}


async def test_markets_response_shape(db_client: AsyncClient, db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    farmer = await make_user(db_session, UserRole.FARMER)
    await make_listing(db_session, farmer, market="Kisumu")
    response = await db_client.get("/api/v1/listings/markets", headers=auth(buyer))
    assert response.status_code == 200
    assert all(set(item) == {"name", "listing_count"} for item in response.json())
    assert "Kisumu" in {item["name"] for item in response.json()}


async def test_markets_agree_with_browse_totals(db_client: AsyncClient, db_session: AsyncSession) -> None:
    buyer = await make_user(db_session, UserRole.BUYER)
    farmer = await make_user(db_session, UserRole.FARMER)
    disabled_farmer = await make_user(db_session, UserRole.FARMER)
    for market in ("Nairobi", "Nairobi", "Kisumu", "Mombasa"):
        await make_listing(db_session, farmer, market=market)
    await make_listing(db_session, farmer, market="Nakuru", status=ListingStatus.SOLD_OUT)
    await make_listing(db_session, farmer, market="Nairobi", status=ListingStatus.REMOVED)
    await make_listing(db_session, disabled_farmer, market="Nakuru")
    await disable(db_session, disabled_farmer)

    counts = await market_counts(db_client, buyer)
    browse = await db_client.get("/api/v1/listings?limit=1", headers=auth(buyer))
    assert browse.status_code == 200, browse.text
    assert sum(counts.values()) == browse.json()["total"]
    for name, count in counts.items():
        filtered = await db_client.get("/api/v1/listings", params={"market": name, "limit": 1}, headers=auth(buyer))
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["total"] == count, name


async def test_markets_run_one_sql_statement(db_session: AsyncSession) -> None:
    farmer = await make_user(db_session, UserRole.FARMER)
    await make_listing(db_session, farmer, market="Nairobi")
    await make_listing(db_session, farmer, market="Kisumu")
    await db_session.execute(select(1))  # open the session's savepoint first so it is not counted

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    connection = db_session.bind.sync_connection
    event.listen(connection, "before_cursor_execute", record)
    try:
        markets = await listing_service.buyer_markets(db_session)
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert len(statements) == 1, statements
    assert "GROUP BY" in statements[0]
    assert {"Nairobi", "Kisumu"} <= {market.name for market in markets}


# One visibility rule -------------------------------------------------------------------------


async def test_browse_markets_and_checkout_agree_on_visibility(db_session: AsyncSession) -> None:
    """One listing per state (active with zero stock is impossible by constraint, so it is skipped)."""
    farmer = await make_user(db_session, UserRole.FARMER)
    disabled_farmer = await make_user(db_session, UserRole.FARMER)
    listings = {
        "active": await make_listing(db_session, farmer, market="Mombasa"),
        "sold_out": await make_listing(db_session, farmer, market="Mombasa", status=ListingStatus.SOLD_OUT),
        "removed": await make_listing(db_session, farmer, market="Mombasa", status=ListingStatus.REMOVED),
        "farmer_disabled": await make_listing(db_session, disabled_farmer, market="Mombasa"),
    }
    before_disable = {market.name: market.listing_count for market in await listing_service.buyer_markets(db_session)}
    await disable(db_session, disabled_farmer)
    expected_visible = {"active"}
    ids = {state: listing.id for state, listing in listings.items()}

    # The loaded-row form of the rule (used by listing detail and checkout).
    row_visible = {
        state
        for state, listing in listings.items()
        if listing_service.listing_is_buyer_visible(listing, farmer_disabled=state == "farmer_disabled")
    }

    page = await listing_service.browse_listings(
        db_session, ListingBrowseFilters(market="Mombasa"), Pagination(limit=100, offset=0)
    )
    browse_ids = {item.id for item in page.items}
    browse_visible = {state for state, listing_id in ids.items() if listing_id in browse_ids}

    markets = {market.name: market.listing_count for market in await listing_service.buyer_markets(db_session)}
    # Disabling the farmer removed exactly one listing (theirs) from the Mombasa count.
    assert before_disable["Mombasa"] - markets.get("Mombasa", 0) == 1
    assert markets.get("Mombasa", 0) == page.total

    # Checkout last, since a successful order changes stock.
    checkout_visible = set()
    for state, listing_id in ids.items():
        buyer = await make_user(db_session, UserRole.BUYER)
        try:
            await order_service.create_order(db_session, buyer, listing_id, Decimal("1"))
        except AppError as exc:
            assert exc.code in {"listing_not_found", "listing_unavailable"}, (state, exc.code)
        else:
            checkout_visible.add(state)

    assert browse_visible == checkout_visible == row_visible == expected_visible
