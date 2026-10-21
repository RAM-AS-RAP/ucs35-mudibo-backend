"""``GET /listings/mine/{listing_id}`` and ``GET /listings/markets`` without a database.

Farmers read one of their own listings in any status. Buyers read the markets that have at least one
listing they could buy, under the same visibility rule as ``GET /listings``.
"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from response_helpers import without_request_id
from sqlalchemy.dialects import postgresql

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import CropType, ListingStatus, UserRole
from app.models.listing import ProduceListing
from app.routers import listings as listings_router
from app.schemas.common import Pagination
from app.schemas.listing import BuyerMarketOut, FarmerOrdersPage, ListingBrowseFilters, ListingOut, ListingPage
from app.services import listing_service, order_service

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
NOT_FOUND = {"error": {"code": "listing_not_found", "message": "Listing not found."}}


def make_user(role: UserRole, **kwargs):
    return SimpleNamespace(
        id=kwargs.pop("id", uuid4()),
        role=role,
        name=f"Test {role.value}",
        is_disabled=False,
        must_change_password=False,
        is_super_admin=kwargs.pop("is_super_admin", False),
    )


def make_listing(farmer_id, **kwargs) -> ProduceListing:
    status = kwargs.pop("status", ListingStatus.ACTIVE)
    return ProduceListing(
        id=kwargs.pop("id", uuid4()),
        farmer_id=farmer_id,
        farmer_role=UserRole.FARMER,
        crop=CropType.MAIZE,
        market=kwargs.pop("market", "Nairobi"),
        quantity_available=kwargs.pop(
            "quantity_available", Decimal("0.00") if status == ListingStatus.SOLD_OUT else Decimal("10.00")
        ),
        unit="kg",
        price_per_unit=Decimal("45.50"),
        image_path=kwargs.pop("image_path", None),
        status=status,
        removed_at=NOW if status == ListingStatus.REMOVED else None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows


class FakeSession:
    """Scripted session that records every statement it is asked to run."""

    def __init__(self, scalars=(), results=()):
        self.scalars = list(scalars)
        self.results = list(results)
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalars.pop(0)

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        return self.results.pop(0)


class UntouchableSession:
    def __getattr__(self, name):
        raise AssertionError(f"the session must not be used ({name})")


def install(application, user, session=None):
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user

    async def db():
        yield session if session is not None else UntouchableSession()

    application.dependency_overrides[get_db] = db


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


async def must_not_run(*args, **kwargs):
    raise AssertionError("this handler must not run")


# Route order ---------------------------------------------------------------------------------


def test_new_static_paths_are_declared_in_the_right_order():
    paths = [getattr(route, "path", "") for route in listings_router.router.routes]
    first_dynamic = paths.index("/listings/{listing_id}")
    mine_item = paths.index("/listings/mine/{listing_id}")
    assert paths.index("/listings/mine") < mine_item
    assert paths.index("/listings/mine/orders") < mine_item
    assert mine_item < first_dynamic
    assert paths.index("/listings/markets") < first_dynamic


def test_listing_id_parameters_are_typed_uuid():
    from uuid import UUID

    for handler in (listings_router.my_listing, listings_router.listing_detail):
        assert inspect.signature(handler).parameters["listing_id"].annotation is UUID


# GET /listings/mine/{listing_id} -------------------------------------------------------------


@pytest.mark.parametrize("status", [ListingStatus.ACTIVE, ListingStatus.SOLD_OUT, ListingStatus.REMOVED])
async def test_owner_reads_own_listing_in_every_status(api_client, application, status):
    farmer = make_user(UserRole.FARMER)
    listing = make_listing(farmer.id, status=status)
    session = FakeSession(scalars=[listing])
    install(application, farmer, session)

    response = await api_client.get(f"/api/v1/listings/mine/{listing.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(listing.id)
    assert body["status"] == status.value
    assert len(session.statements) == 1


async def test_single_listing_query_is_a_plain_owner_filtered_select():
    farmer = make_user(UserRole.FARMER)
    listing_id = uuid4()
    session = FakeSession(scalars=[make_listing(farmer.id, id=listing_id)])
    await listing_service.get_owned_listing(session, farmer, listing_id)
    statement = compiled(session.statements[0])
    sql = str(statement)
    assert "produce_listings.id = " in sql and "produce_listings.farmer_id = " in sql
    assert "FOR UPDATE" not in sql
    assert {listing_id, farmer.id} <= set(statement.params.values())
    for forbidden in ("orders", "payments", "users"):
        assert f"FROM {forbidden}" not in sql and f"JOIN {forbidden}" not in sql


async def test_foreign_and_unknown_listing_are_identical_404s(api_client, application):
    # The owner filter is in the query, so another farmer's listing and an unknown id both find nothing.
    install(application, make_user(UserRole.FARMER), FakeSession(scalars=[None, None]))
    foreign = await api_client.get(f"/api/v1/listings/mine/{uuid4()}")
    unknown = await api_client.get(f"/api/v1/listings/mine/{uuid4()}")
    assert foreign.status_code == unknown.status_code == 404
    assert without_request_id(foreign.json()) == without_request_id(unknown.json()) == NOT_FOUND


@pytest.mark.parametrize(
    ("role", "super_admin"), [(UserRole.BUYER, False), (UserRole.ADMIN, False), (UserRole.ADMIN, True)]
)
async def test_buyers_and_admins_are_forbidden_from_the_single_listing(
    api_client, application, monkeypatch, role, super_admin
):
    monkeypatch.setattr(listing_service, "get_owned_listing", must_not_run)
    install(application, make_user(role, is_super_admin=super_admin))
    response = await api_client.get(f"/api/v1/listings/mine/{uuid4()}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_anonymous_single_listing_is_401(api_client, application):
    install(application, None)
    response = await api_client.get(f"/api/v1/listings/mine/{uuid4()}")
    assert response.status_code == 401


async def test_non_uuid_single_listing_is_422(api_client, application, monkeypatch):
    monkeypatch.setattr(listing_service, "get_owned_listing", must_not_run)
    install(application, make_user(UserRole.FARMER))
    response = await api_client.get("/api/v1/listings/mine/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_mine_and_mine_orders_still_reach_their_own_handlers(api_client, application, monkeypatch):
    farmer = make_user(UserRole.FARMER)
    install(application, farmer)
    reached = []

    async def my_listings(session, user, pagination, status_filter=None):
        reached.append("mine")
        return ListingPage(items=[], total=0, limit=pagination.limit, offset=pagination.offset)

    async def farmer_orders(session, user, pagination, status_filter=None, listing_id=None):
        reached.append("orders")
        return FarmerOrdersPage(items=[], total=0, limit=pagination.limit, offset=pagination.offset, note="n")

    monkeypatch.setattr(listing_service, "get_owned_listing", must_not_run)
    monkeypatch.setattr(listing_service, "my_listings", my_listings)
    monkeypatch.setattr(order_service, "list_orders_for_farmer", farmer_orders)

    mine = await api_client.get("/api/v1/listings/mine")
    orders = await api_client.get("/api/v1/listings/mine/orders")

    assert [mine.status_code, orders.status_code] == [200, 200]
    assert reached == ["mine", "orders"]
    assert orders.json()["note"] == "n"


async def test_single_listing_has_the_same_keys_and_image_url_as_mine_items(api_client, application, monkeypatch):
    monkeypatch.setattr(listing_service, "public_url", lambda path: f"https://storage.test/{path}")
    farmer = make_user(UserRole.FARMER)
    listing = make_listing(farmer.id, image_path="listings/abc/photo.jpg")

    class MineSession:
        """GET /listings/mine runs a count (scalar) and then the page query (scalars)."""

        async def scalar(self, statement):
            return 1

        async def scalars(self, statement):
            return [listing]

    install(application, farmer, MineSession())
    mine = await api_client.get("/api/v1/listings/mine")
    assert mine.status_code == 200, mine.text
    mine_item = mine.json()["items"][0]

    install(application, farmer, FakeSession(scalars=[listing]))
    single = await api_client.get(f"/api/v1/listings/mine/{listing.id}")
    assert single.status_code == 200, single.text

    assert set(single.json()) == set(mine_item) == set(ListingOut.model_fields)
    assert single.json() == mine_item
    assert single.json()["image_url"] == "https://storage.test/listings/abc/photo.jpg"


async def test_single_listing_response_has_no_buyer_or_order_data(api_client, application):
    farmer = make_user(UserRole.FARMER)
    listing = make_listing(farmer.id)
    install(application, farmer, FakeSession(scalars=[listing]))
    body = (await api_client.get(f"/api/v1/listings/mine/{listing.id}")).json()
    for forbidden in ("buyer", "buyer_id", "buyer_name", "orders", "order_id", "phone_number", "email", "payment"):
        assert forbidden not in body


# GET /listings/markets -----------------------------------------------------------------------


async def test_markets_returns_name_and_count_only(api_client, application):
    session = FakeSession(results=[FakeResult([("Kisumu", 2), ("Nairobi", 5)])])
    install(application, make_user(UserRole.BUYER), session)
    response = await api_client.get("/api/v1/listings/markets")
    assert response.status_code == 200, response.text
    assert response.json() == [{"name": "Kisumu", "listing_count": 2}, {"name": "Nairobi", "listing_count": 5}]
    assert all(set(item) == {"name", "listing_count"} for item in response.json())
    assert set(BuyerMarketOut.model_fields) == {"name", "listing_count"}


async def test_markets_with_nothing_listed_is_an_empty_list(api_client, application):
    install(application, make_user(UserRole.BUYER), FakeSession(results=[FakeResult([])]))
    response = await api_client.get("/api/v1/listings/markets")
    assert response.status_code == 200
    assert response.json() == []


async def test_markets_is_not_parsed_as_a_listing_id(api_client, application, monkeypatch):
    monkeypatch.setattr(listing_service, "get_public_listing", must_not_run)
    install(application, make_user(UserRole.BUYER), FakeSession(results=[FakeResult([])]))
    response = await api_client.get("/api/v1/listings/markets")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("role", "super_admin"), [(UserRole.FARMER, False), (UserRole.ADMIN, False), (UserRole.ADMIN, True)]
)
async def test_farmers_and_admins_are_forbidden_from_markets(api_client, application, monkeypatch, role, super_admin):
    monkeypatch.setattr(listing_service, "buyer_markets", must_not_run)
    install(application, make_user(role, is_super_admin=super_admin))
    response = await api_client.get("/api/v1/listings/markets")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_anonymous_markets_is_401(api_client, application):
    install(application, None)
    response = await api_client.get("/api/v1/listings/markets")
    assert response.status_code == 401


async def test_markets_runs_one_grouped_statement_with_the_browse_visibility_rule():
    session = FakeSession(results=[FakeResult([])])
    await listing_service.buyer_markets(session)
    assert len(session.statements) == 1
    statement = compiled(session.statements[0])
    sql = str(statement)
    assert "JOIN users ON users.id = produce_listings.farmer_id" in sql
    assert "GROUP BY produce_listings.market" in sql
    assert "ORDER BY produce_listings.market ASC" in sql
    assert "produce_listings.status = %(status_1)s" in sql
    assert "produce_listings.quantity_available > %(quantity_available_1)s" in sql
    assert "is_disabled IS true" in sql
    assert statement.params["status_1"] == ListingStatus.ACTIVE
    assert statement.params["quantity_available_1"] == 0
    for forbidden in ("market_prices", "price_per_unit", "latest_month"):
        assert forbidden not in sql


async def test_browse_and_markets_compile_the_same_visibility_clause():
    session = FakeSession(scalars=[0], results=[FakeResult([]), FakeResult([])])
    await listing_service.browse_listings(session, ListingBrowseFilters(), Pagination(limit=20, offset=0))
    await listing_service.buyer_markets(session)
    count_query, page_query, markets_query = session.statements
    fragments = {_visibility_fragment(statement) for statement in (count_query, page_query, markets_query)}
    assert len(fragments) == 1, fragments
    assert "quantity_available >" in fragments.pop()


def _visibility_fragment(statement) -> str:
    """The WHERE text from the status condition through the disabled-farmer EXISTS."""
    sql = " ".join(str(compiled(statement)).split())
    start = sql.index("produce_listings.status = ")
    end = sql.index("IS true)", start) + len("IS true)")
    return sql[start:end]


# The shared rule on loaded rows --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "farmer_disabled", "visible"),
    [
        (ListingStatus.ACTIVE, False, True),
        (ListingStatus.ACTIVE, True, False),
        (ListingStatus.SOLD_OUT, False, False),
        (ListingStatus.REMOVED, False, False),
    ],
)
def test_loaded_row_rule_matches_the_clause(status, farmer_disabled, visible):
    listing = make_listing(uuid4(), status=status)
    assert listing_service.listing_is_buyer_visible(listing, farmer_disabled=farmer_disabled) is visible


def test_detail_and_checkout_use_the_shared_rule():
    assert "listing_is_buyer_visible" in inspect.getsource(listing_service.get_public_listing)
    assert "buyer_visible_listings_clause" in inspect.getsource(listing_service.browse_listings)
    assert "buyer_visible_listings_clause" in inspect.getsource(listing_service.buyer_markets)
    checkout = inspect.getsource(order_service.create_order)
    assert "listing_is_buyer_visible" in checkout and "farmer_not_disabled" in checkout
