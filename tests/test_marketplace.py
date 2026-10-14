"""Buyer marketplace tests: browse, detail, checkout, order views, and cancel, without a database."""

import inspect
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_current_user
from app.core.errors import AppError
from app.db import get_db
from app.models.enums import CropType, ListingStatus, OrderStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.schemas.common import Pagination
from app.schemas.listing import ListingBrowseFilters, ListingSort, PublicListingOut
from app.schemas.order import ORDER_NOTE, OrderOut, OrderPage, OrderPaymentSummary
from app.services import listing_service, order_service, payment_service

FIXED_NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def make_user(role: UserRole = UserRole.BUYER, **kwargs):
    return SimpleNamespace(
        id=kwargs.pop("id", uuid4()),
        role=role,
        name=kwargs.pop("name", f"Test {role.value}"),
        is_disabled=False,
        must_change_password=False,
        is_super_admin=False,
    )


def install_user(application, role=None, session=None):
    user = make_user(role) if role is not None else None
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user

    async def db():
        yield session if session is not None else object()

    application.dependency_overrides[get_db] = db
    return user


def make_listing(**kwargs) -> ProduceListing:
    return ProduceListing(
        id=kwargs.pop("id", uuid4()),
        farmer_id=kwargs.pop("farmer_id", uuid4()),
        farmer_role=UserRole.FARMER,
        crop=kwargs.pop("crop", CropType.MAIZE),
        market=kwargs.pop("market", "Nairobi"),
        quantity_available=kwargs.pop("quantity_available", Decimal("10.00")),
        unit="kg",
        price_per_unit=kwargs.pop("price_per_unit", Decimal("40.00")),
        image_path=kwargs.pop("image_path", None),
        status=kwargs.pop("status", ListingStatus.ACTIVE),
        removed_at=kwargs.pop("removed_at", None),
        created_at=kwargs.pop("created_at", FIXED_NOW),
        updated_at=FIXED_NOW,
    )


def make_order(buyer_id, listing_id=None, **kwargs) -> Order:
    return Order(
        id=kwargs.pop("id", uuid4()),
        reference="ABCD2345",
        listing_id=listing_id or uuid4(),
        buyer_id=buyer_id,
        buyer_role=UserRole.BUYER,
        quantity=kwargs.pop("quantity", Decimal("4.00")),
        unit_price=Decimal("40.00"),
        total_amount=160,
        status=kwargs.pop("status", OrderStatus.PENDING_PAYMENT),
        expires_at=kwargs.pop("expires_at", FIXED_NOW + timedelta(minutes=5)),
    )


def view_row(**overrides):
    """A row shaped like the joined order view query, with a fixed database clock."""
    row = {
        "id": uuid4(),
        "reference": "ABCD2345",
        "buyer_id": uuid4(),
        "status": "pending_payment",
        "quantity": Decimal("2.50"),
        "unit_price": Decimal("112.56"),
        "total_amount": 282,
        "expires_at": FIXED_NOW + timedelta(minutes=5),
        "created_at": FIXED_NOW,
        "listing_id": uuid4(),
        "crop": "maize",
        "market": "Nairobi",
        "unit": "kg",
        "image_path": None,
        "listing_status": "active",
        "seller_name": "Farmer Jane",
        "payment_status": None,
        "mpesa_receipt_number": None,
        "result_desc": None,
        "payment_completed_at": None,
        "payment_attempts": 0,
        "has_open_payment": False,
        "server_time": FIXED_NOW,
    }
    row.update(overrides)
    return row


class FakeResult:
    def __init__(self, rows=None, mapping_rows=None):
        self.rows = list(rows or [])
        self.mapping_rows = list(mapping_rows or [])

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def mappings(self):
        return SimpleNamespace(
            first=lambda: self.mapping_rows[0] if self.mapping_rows else None, all=lambda: self.mapping_rows
        )


class FakeSession:
    """Scripted async session that records every statement in execution order."""

    def __init__(self, scalars=(), results=(), collisions=0, collision_error=None):
        self.scalars = list(scalars)
        self.results = list(results)
        self.collisions = collisions
        self.collision_error = collision_error
        self.statements = []
        self.added = []
        self.commits = 0
        self.flushes = 0
        self.nested = 0

    def _record(self, statement):
        self.statements.append(statement)

    async def scalar(self, statement):
        self._record(statement)
        return self.scalars.pop(0)

    async def execute(self, statement, params=None):
        self._record(statement)
        return self.results.pop(0)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    @asynccontextmanager
    async def begin_nested(self):
        self.nested += 1
        yield self
        if self.collisions > 0:
            self.collisions -= 1
            raise self.collision_error or IntegrityError(
                "insert into orders",
                {},
                Exception('duplicate key value violates unique constraint "orders_reference_key"'),
            )


def compiled(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def order_out(**overrides) -> OrderOut:
    """Build an order view as seen by its owning buyer."""
    overrides.setdefault("buyer_id", uuid4())
    return order_service._order_out(view_row(**overrides), make_user(id=overrides["buyer_id"]))


# Role matrix ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.ADMIN])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/orders", {"listing_id": str(uuid4()), "quantity": "1"}),
        ("get", "/api/v1/orders/mine", None),
        ("post", f"/api/v1/orders/{uuid4()}/cancel", None),
    ],
)
async def test_farmer_and_admin_are_forbidden_from_buyer_order_routes(
    api_client, application, role, method, path, body
):
    install_user(application, role)
    response = await (api_client.post(path, json=body) if method == "post" else api_client.get(path))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_buyer_is_forbidden_from_farmer_listing_history(api_client, application):
    install_user(application, UserRole.BUYER)
    response = await api_client.get("/api/v1/listings/mine")
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/listings"),
        ("get", f"/api/v1/listings/{uuid4()}"),
        ("get", "/api/v1/orders/mine"),
        ("get", f"/api/v1/orders/{uuid4()}"),
        ("post", "/api/v1/orders"),
        ("post", f"/api/v1/orders/{uuid4()}/cancel"),
    ],
)
async def test_missing_token_is_401_for_marketplace_routes(api_client, application, method, path):
    install_user(application)
    response = await getattr(api_client, method)(path)
    assert response.status_code == 401


async def test_admin_reads_any_order_without_pay_or_cancel_actions(api_client, application):
    other_buyer_id = uuid4()
    order = make_order(other_buyer_id)
    session = FakeSession(
        scalars=[order],
        results=[FakeResult(mapping_rows=[view_row(id=order.id, buyer_id=other_buyer_id)])],
    )
    install_user(application, UserRole.ADMIN, session)
    response = await api_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["can_pay"] is False
    assert body["can_cancel"] is False


def test_pay_endpoint_is_not_registered_in_this_module(application):
    paths = application.openapi()["paths"]
    assert "/api/v1/orders/{order_id}/pay" not in paths
    assert paths["/api/v1/orders"].keys() == {"post"}
    assert paths["/api/v1/orders/{order_id}/cancel"].keys() == {"post"}


# Browse --------------------------------------------------------------------------------------


async def browse(filters=None, pagination=None, market_lookup=()):
    session = FakeSession(scalars=[*market_lookup, 0], results=[FakeResult(rows=[])])
    await listing_service.browse_listings(
        session, filters or ListingBrowseFilters(), pagination or Pagination(limit=20, offset=0)
    )
    return session


async def test_browse_returns_only_active_listings_with_stock():
    session = await browse()
    count_sql, page_sql = compiled(session.statements[0]), compiled(session.statements[1])
    for sql in (count_sql, page_sql):
        assert "produce_listings.status = %(status_1)s" in sql
        assert "produce_listings.quantity_available > %(quantity_available_1)s" in sql
    params = session.statements[1].compile(dialect=postgresql.dialect()).params
    assert params["status_1"] == ListingStatus.ACTIVE
    assert params["quantity_available_1"] == 0
    assert "JOIN users ON users.id = produce_listings.farmer_id" in page_sql


async def test_browse_applies_crop_market_and_price_filters():
    filters = ListingBrowseFilters(
        crop=CropType.BEANS, market=" kisumu ", min_price=Decimal("10"), max_price=Decimal("50")
    )
    session = await browse(filters, market_lookup=["Kisumu"])
    page = session.statements[2].compile(dialect=postgresql.dialect())
    sql = str(page)
    assert "produce_listings.market = " in sql
    assert "produce_listings.crop = " in sql
    assert "produce_listings.price_per_unit >= " in sql
    assert "produce_listings.price_per_unit <= " in sql
    assert {"Kisumu", CropType.BEANS, Decimal("10"), Decimal("50")} <= set(page.params.values())


async def test_browse_rejects_unknown_market_with_422():
    session = FakeSession(scalars=[None])
    with pytest.raises(AppError) as exc_info:
        await listing_service.browse_listings(session, ListingBrowseFilters(market="Atlantis"), Pagination())
    assert exc_info.value.status_code == 422
    assert "market_locations" in compiled(session.statements[0])


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (ListingSort.NEWEST, "ORDER BY produce_listings.created_at DESC, produce_listings.id ASC"),
        (ListingSort.PRICE_ASC, "ORDER BY produce_listings.price_per_unit ASC, produce_listings.id ASC"),
        (ListingSort.PRICE_DESC, "ORDER BY produce_listings.price_per_unit DESC, produce_listings.id ASC"),
    ],
)
async def test_browse_sort_orders_have_a_stable_id_tie_breaker(sort, expected):
    session = await browse(ListingBrowseFilters(sort=sort))
    assert expected in compiled(session.statements[1])


async def test_browse_applies_pagination_to_the_page_query():
    session = await browse(pagination=Pagination(limit=5, offset=10))
    page = session.statements[1].compile(dialect=postgresql.dialect())
    assert "LIMIT" in str(page) and "OFFSET" in str(page)
    assert page.params["param_1"] == 5
    assert page.params["param_2"] == 10


@pytest.mark.parametrize(
    "query",
    [
        "min_price=50&max_price=10",
        "min_price=-1",
        "max_price=NaN",
        "limit=0",
        "limit=101",
        "offset=-1",
        "sort=cheapest",
        "crop=rice",
    ],
)
async def test_browse_rejects_invalid_query_parameters(api_client, application, monkeypatch, query):
    install_user(application, UserRole.BUYER)

    async def must_not_run(*args):
        raise AssertionError("service must not run")

    monkeypatch.setattr(listing_service, "browse_listings", must_not_run)
    response = await api_client.get(f"/api/v1/listings?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_browse_unknown_market_is_422_through_the_route(api_client, application):
    install_user(application, UserRole.BUYER, FakeSession(scalars=[None]))
    response = await api_client.get("/api/v1/listings?market=Atlantis")
    assert response.status_code == 422


async def test_browse_response_hides_farmer_id_and_contact_details(api_client, application):
    farmer_id = uuid4()
    listing = make_listing(farmer_id=farmer_id)
    session = FakeSession(scalars=[1], results=[FakeResult(rows=[(listing, "Farmer Jane")])])
    install_user(application, UserRole.BUYER, session)
    response = await api_client.get("/api/v1/listings")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["seller_name"] == "Farmer Jane"
    assert item["price_per_unit"] == 40.0
    for forbidden in ("farmer_id", "phone", "phone_number", "email"):
        assert forbidden not in item
    assert str(farmer_id) not in response.text
    assert set(PublicListingOut.model_fields) == {
        "id",
        "crop",
        "market",
        "quantity_available",
        "unit",
        "price_per_unit",
        "image_url",
        "seller_name",
        "status",
        "created_at",
    }


# Detail --------------------------------------------------------------------------------------


async def test_sold_out_listing_detail_is_not_available():
    listing = make_listing(quantity_available=Decimal("0.00"), status=ListingStatus.SOLD_OUT)
    session = FakeSession(results=[FakeResult(rows=[(listing, "Farmer Jane")])])
    detail = await listing_service.get_public_listing(session, listing.id)
    assert detail.available is False
    assert detail.status == "sold_out"
    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert params["status_1"] == [ListingStatus.ACTIVE, ListingStatus.SOLD_OUT]


async def test_active_listing_detail_is_available():
    listing = make_listing()
    detail = await listing_service.get_public_listing(
        FakeSession(results=[FakeResult(rows=[(listing, "Farmer Jane")])]), listing.id
    )
    assert detail.available is True


async def test_removed_and_unknown_listing_details_are_identical_404s(api_client, application):
    install_user(application, UserRole.BUYER, FakeSession(results=[FakeResult(), FakeResult()]))
    removed = await api_client.get(f"/api/v1/listings/{uuid4()}")
    unknown = await api_client.get(f"/api/v1/listings/{uuid4()}")
    assert removed.status_code == unknown.status_code == 404
    assert removed.json() == unknown.json() == {"error": {"code": "listing_not_found", "message": "Listing not found."}}


# Order validation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "NaN"},
        {"quantity": "1.234"},
        {"quantity": "0.5"},
        {"quantity": "100000.01"},
        {"quantity": True},
        {"quantity": "abc"},
        {"quantity": "1", "note": "hi"},
        {"quantity": "1", "unit_price": "1"},
        {"quantity": "1", "total_amount": 1},
        {"quantity": "1", "buyer_id": str(uuid4())},
    ],
)
async def test_order_body_validation_rejects_bad_input(api_client, application, monkeypatch, body):
    install_user(application, UserRole.BUYER)

    async def must_not_run(*args):
        raise AssertionError("service must not run")

    monkeypatch.setattr(order_service, "create_order", must_not_run)
    response = await api_client.post("/api/v1/orders", json={"listing_id": str(uuid4()), **body})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(("raw", "expected"), [("2.5", Decimal("2.50")), (3, Decimal("3.00")), (1.25, Decimal("1.25"))])
async def test_order_quantity_accepts_strings_and_numbers(api_client, application, monkeypatch, raw, expected):
    install_user(application, UserRole.BUYER)
    received = {}

    async def create_order(session, buyer, listing_id, quantity):
        received["quantity"] = quantity
        return order_out(buyer_id=buyer.id)

    monkeypatch.setattr(order_service, "create_order", create_order)
    response = await api_client.post("/api/v1/orders", json={"listing_id": str(uuid4()), "quantity": raw})
    assert response.status_code == 201, response.text
    assert received["quantity"] == expected


# Totals --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "price", "expected"),
    [("2.5", "112.56", 282), ("1", "40.00", 40), ("0.01", "40", 1), ("2", "50", 100)],
)
def test_total_is_ceiling_of_decimal_product(quantity, price, expected):
    total = order_service.calculate_total(Decimal(quantity), Decimal(price))
    assert total == expected
    assert type(total) is int


def test_total_helper_uses_decimal_and_never_float():
    source = inspect.getsource(order_service.calculate_total)
    assert "ROUND_CEILING" in source
    assert "float" not in source


async def test_tiny_order_below_minimum_total_is_rejected():
    listing = make_listing()
    session = FakeSession(scalars=[listing], results=[FakeResult(rows=[])])
    with pytest.raises(AppError) as exc_info:
        await order_service.create_order(session, make_user(), listing.id, Decimal("0.01"))
    assert (exc_info.value.status_code, exc_info.value.code) == (422, "order_too_small")
    assert listing.quantity_available == Decimal("10.00")
    assert session.added == []


# Checkout ------------------------------------------------------------------------------------


@pytest.fixture
def fake_view(monkeypatch):
    calls = []

    async def fetch(session, order_id, viewer):
        calls.append(order_id)
        return order_out(id=order_id, buyer_id=viewer.id)

    monkeypatch.setattr(order_service, "_fetch_order_view", fetch)
    return calls


async def checkout(listing, quantity="4", pending=(), collisions=0, buyer=None):
    session = FakeSession(scalars=[listing], results=[FakeResult(rows=list(pending))], collisions=collisions)
    result = await order_service.create_order(session, buyer or make_user(), listing.id, Decimal(quantity))
    return session, result


async def test_checkout_reserves_stock_and_snapshots_server_price(fake_view):
    listing = make_listing(quantity_available=Decimal("10.00"), price_per_unit=Decimal("40.00"))
    buyer = make_user()
    session, _ = await checkout(listing, "4", buyer=buyer)
    order = session.added[0]
    assert listing.quantity_available == Decimal("6.00")
    assert listing.status == ListingStatus.ACTIVE
    assert order.unit_price == Decimal("40.00")
    assert order.total_amount == 160
    assert order.buyer_id == buyer.id
    assert order.buyer_role == UserRole.BUYER
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert re.fullmatch(r"[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}", order.reference)
    assert session.commits == 1
    assert fake_view == [order.id]


async def test_checkout_locks_only_the_listing(fake_view):
    session, _ = await checkout(make_listing())
    locked = [compiled(statement) for statement in session.statements if "FOR UPDATE" in compiled(statement)]
    assert len(locked) == 1
    assert "FROM produce_listings" in locked[0]


async def test_ordering_the_full_stock_marks_the_listing_sold_out(fake_view):
    listing = make_listing(quantity_available=Decimal("4.00"))
    await checkout(listing, "4")
    assert listing.quantity_available == Decimal("0.00")
    assert listing.status == ListingStatus.SOLD_OUT


async def test_insufficient_stock_is_409_with_the_available_quantity():
    listing = make_listing(quantity_available=Decimal("3.00"))
    with pytest.raises(AppError) as exc_info:
        await checkout(listing, "4")
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "insufficient_stock")
    assert "3.00" in exc_info.value.message
    assert listing.quantity_available == Decimal("3.00")


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (None, (404, "listing_not_found")),
        (
            make_listing(status=ListingStatus.REMOVED, removed_at=FIXED_NOW),
            (404, "listing_not_found"),
        ),
        (make_listing(status=ListingStatus.SOLD_OUT, quantity_available=Decimal("0.00")), (409, "listing_unavailable")),
    ],
)
async def test_missing_removed_or_sold_out_listing_is_rejected(listing, expected):
    session = FakeSession(scalars=[listing])
    with pytest.raises(AppError) as exc_info:
        await order_service.create_order(session, make_user(), uuid4(), Decimal("1"))
    assert (exc_info.value.status_code, exc_info.value.code) == expected


async def test_existing_pending_order_for_same_listing_returns_its_id():
    listing = make_listing()
    existing_id = uuid4()
    with pytest.raises(AppError) as exc_info:
        await checkout(listing, "1", pending=[(existing_id, listing.id)])
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "pending_order_exists")
    assert exc_info.value.extra == {"existing_order_id": str(existing_id)}


async def test_pending_order_exists_body_includes_existing_order_id(api_client, application, monkeypatch):
    install_user(application, UserRole.BUYER)
    existing_id = str(uuid4())

    async def create_order(*args):
        raise AppError(409, "pending_order_exists", "Pending.", extra={"existing_order_id": existing_id})

    monkeypatch.setattr(order_service, "create_order", create_order)
    response = await api_client.post("/api/v1/orders", json={"listing_id": str(uuid4()), "quantity": "1"})
    assert response.status_code == 409
    assert response.json()["error"]["existing_order_id"] == existing_id


async def test_pending_order_limit_is_enforced():
    pending = [(uuid4(), uuid4()) for _ in range(3)]
    with pytest.raises(AppError) as exc_info:
        await checkout(make_listing(), "1", pending=pending)
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "too_many_pending_orders")


async def test_expired_pending_orders_do_not_count_toward_limits(fake_view):
    listing = make_listing()
    # The database filters expired orders out (expires_at > now()), so none are returned here.
    session, _ = await checkout(listing, "1", pending=[])
    pending_sql = compiled(session.statements[1])
    assert "orders.expires_at > now()" in pending_sql
    assert "orders.status = " in pending_sql
    assert "FOR UPDATE" not in pending_sql


async def test_reference_collision_retries_once_and_succeeds(fake_view):
    session, _ = await checkout(make_listing(), "1", collisions=1)
    assert session.nested == 2
    assert len(session.added) == 2
    assert session.commits == 1


async def test_five_reference_collisions_give_a_500():
    session = FakeSession(scalars=[make_listing()], results=[FakeResult()], collisions=5)
    with pytest.raises(AppError) as exc_info:
        await order_service.create_order(session, make_user(), uuid4(), Decimal("1"))
    assert exc_info.value.status_code == 500
    assert session.nested == 5
    assert session.commits == 0


async def test_other_integrity_errors_are_not_retried():
    error = IntegrityError("insert", {}, Exception('violates check constraint "orders_total_positive"'))
    session = FakeSession(scalars=[make_listing()], results=[FakeResult()], collisions=1, collision_error=error)
    with pytest.raises(IntegrityError):
        await order_service.create_order(session, make_user(), uuid4(), Decimal("1"))
    assert session.nested == 1


async def test_expires_at_uses_the_database_clock(fake_view):
    session, _ = await checkout(make_listing(), "1")
    expires_at = compiled(session.added[0].expires_at)
    assert "now()" in expires_at
    source = inspect.getsource(order_service)
    assert "datetime.now" not in source
    assert "utcnow" not in source


# Order views ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expires_delta", "open_payment", "expected"),
    [
        ("pending_payment", 5, False, (False, True, True)),
        ("pending_payment", 5, True, (False, False, False)),
        ("pending_payment", -1, False, (True, False, True)),
        ("pending_payment", 0, False, (True, False, True)),
        ("pending_payment", -1, True, (True, False, False)),
        ("paid", 5, False, (False, False, False)),
        ("expired", -1, False, (False, False, False)),
        ("cancelled", 5, False, (False, False, False)),
    ],
)
def test_expiry_and_action_flags(status, expires_delta, open_payment, expected):
    out = order_out(
        status=status,
        expires_at=FIXED_NOW + timedelta(minutes=expires_delta),
        has_open_payment=open_payment,
    )
    assert (out.is_expired, out.can_pay, out.can_cancel) == expected


def test_non_owner_viewer_gets_no_actions():
    row = view_row()
    out = order_service._order_out(row, make_user(UserRole.BUYER))
    assert (out.can_pay, out.can_cancel) == (False, False)
    admin = order_service._order_out(row, make_user(UserRole.ADMIN))
    assert (admin.can_pay, admin.can_cancel) == (False, False)


def test_payment_summary_never_exposes_identifiers_or_phones():
    out = order_out(
        payment_status="completed",
        mpesa_receipt_number="SBX123",
        result_desc="The service request is processed successfully.",
        payment_completed_at=FIXED_NOW,
        payment_attempts=2,
    )
    payload = json.loads(out.model_dump_json())
    assert payload["payment"] == {
        "status": "completed",
        "mpesa_receipt_number": "SBX123",
        "result_desc": "The service request is processed successfully.",
        "completed_at": "2026-10-13T09:00:00Z",
        "attempts": 2,
    }
    assert set(OrderPaymentSummary.model_fields) == {
        "status",
        "mpesa_receipt_number",
        "result_desc",
        "completed_at",
        "attempts",
    }
    text = json.dumps(payload)
    for forbidden in ("idempotency_key", "checkout_request_id", "merchant_request_id", "phone"):
        assert forbidden not in text
    assert payload["note"] == ORDER_NOTE
    assert payload["unit_price"] == 112.56
    assert payload["total_amount"] == 282
    assert payload["server_time"] == "2026-10-13T09:00:00Z"


def test_order_without_payment_has_null_payment():
    assert order_out().payment is None


async def test_owner_sees_order_and_reconcile_is_called_once(monkeypatch):
    buyer = make_user()
    order = make_order(buyer.id)
    calls = []

    async def reconcile(session, seen_order):
        calls.append(seen_order)

    monkeypatch.setattr(payment_service, "maybe_reconcile_payment", reconcile)
    session = FakeSession(
        scalars=[order], results=[FakeResult(mapping_rows=[view_row(id=order.id, buyer_id=buyer.id)])]
    )
    out = await order_service.get_order(session, order.id, buyer)
    assert out.id == order.id
    assert out.can_pay is True
    assert calls == [order]


async def test_maybe_reconcile_payment_does_nothing():
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"session.{name} must not be used")

    assert await payment_service.maybe_reconcile_payment(Untouchable(), make_order(uuid4())) is None


async def test_other_buyers_order_is_the_same_404_as_a_missing_one(api_client, application):
    session = FakeSession(scalars=[make_order(uuid4()), None])
    install_user(application, UserRole.BUYER, session)
    foreign = await api_client.get(f"/api/v1/orders/{uuid4()}")
    missing = await api_client.get(f"/api/v1/orders/{uuid4()}")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"error": {"code": "order_not_found", "message": "Order not found."}}


async def test_my_orders_lists_own_orders_in_one_query():
    buyer = make_user()
    rows = [dict(view_row(buyer_id=buyer.id), total=2), dict(view_row(buyer_id=buyer.id), total=2)]
    session = FakeSession(results=[FakeResult(mapping_rows=rows)])
    page = await order_service.list_my_orders(session, buyer, Pagination(limit=20, offset=0), OrderStatus.PAID)
    assert isinstance(page, OrderPage)
    assert page.total == 2 and len(page.items) == 2
    assert len(session.statements) == 1
    sql = str(session.statements[0])
    assert "o.buyer_id = cast(:buyer_id as uuid)" in sql
    assert "order by created_at desc" in sql


async def test_my_orders_empty_page():
    rows = [dict(view_row(id=None), total=0)]
    page = await order_service.list_my_orders(
        FakeSession(results=[FakeResult(mapping_rows=rows)]), make_user(), Pagination()
    )
    assert page.items == [] and page.total == 0


# Cancel --------------------------------------------------------------------------------------


async def cancel(order, listing=None, open_payment=None):
    session = FakeSession(scalars=[order, open_payment, listing])
    buyer = make_user(id=order.buyer_id)
    await order_service.cancel_order(session, buyer, order.id)
    return session


async def test_cancel_restores_stock_and_reactivates_sold_out_listing(fake_view):
    listing = make_listing(quantity_available=Decimal("0.00"), status=ListingStatus.SOLD_OUT)
    order = make_order(uuid4(), listing.id, quantity=Decimal("4.00"))
    session = await cancel(order, listing)
    assert order.status == OrderStatus.CANCELLED
    assert listing.quantity_available == Decimal("4.00")
    assert listing.status == ListingStatus.ACTIVE
    assert session.commits == 1


async def test_cancel_restores_quantity_on_removed_listing_without_reactivating(fake_view):
    listing = make_listing(quantity_available=Decimal("1.00"), status=ListingStatus.REMOVED, removed_at=FIXED_NOW)
    order = make_order(uuid4(), listing.id, quantity=Decimal("2.00"))
    await cancel(order, listing)
    assert listing.quantity_available == Decimal("3.00")
    assert listing.status == ListingStatus.REMOVED


async def test_cancel_locks_the_order_before_the_listing(fake_view):
    listing = make_listing()
    order = make_order(uuid4(), listing.id)
    session = await cancel(order, listing)
    sql = [compiled(statement) for statement in session.statements]
    order_lock = next(i for i, s in enumerate(sql) if "FROM orders" in s and "FOR UPDATE" in s)
    listing_lock = next(i for i, s in enumerate(sql) if "FROM produce_listings" in s and "FOR UPDATE" in s)
    assert order_lock == 0
    assert order_lock < listing_lock


async def test_cancel_is_idempotent_for_an_already_cancelled_order(fake_view):
    order = make_order(uuid4(), status=OrderStatus.CANCELLED)
    session = FakeSession(scalars=[order])
    await order_service.cancel_order(session, make_user(id=order.buyer_id), order.id)
    assert session.commits == 0
    assert len(session.statements) == 1
    assert fake_view == [order.id]


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.EXPIRED, OrderStatus.NEEDS_RECONCILIATION])
async def test_cancel_rejects_orders_that_are_not_pending(status):
    order = make_order(uuid4(), status=status)
    with pytest.raises(AppError) as exc_info:
        await order_service.cancel_order(FakeSession(scalars=[order]), make_user(id=order.buyer_id), order.id)
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "order_not_cancellable")


async def test_cancel_rejects_an_order_with_an_open_payment():
    listing = make_listing()
    order = make_order(uuid4(), listing.id)
    with pytest.raises(AppError) as exc_info:
        await cancel(order, listing, open_payment=uuid4())
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "payment_in_progress")
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert listing.quantity_available == Decimal("10.00")


async def test_cancel_of_another_buyers_order_is_404(api_client, application):
    session = FakeSession(scalars=[None])
    install_user(application, UserRole.BUYER, session)
    response = await api_client.post(f"/api/v1/orders/{uuid4()}/cancel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_not_found"
    assert "orders.buyer_id = " in compiled(session.statements[0])


# Source checks -------------------------------------------------------------------------------

CHANGED_FILES = [
    "services/order_service.py",
    "services/listing_service.py",
    "schemas/order.py",
    "schemas/listing.py",
    "routers/orders.py",
    "routers/listings.py",
]


def test_order_and_listing_services_do_not_use_float_for_money():
    for name in ("services/order_service.py", "services/listing_service.py"):
        assert "float(" not in (APP_DIR / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", CHANGED_FILES)
def test_changed_files_import_no_ml_or_daraja_code(name):
    source = (APP_DIR / name).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines() if line.strip().startswith(("import ", "from "))]
    for line in imports:
        for banned in ("sklearn", "xgboost", "shap", "daraja"):
            assert banned not in line.lower(), f"{name}: {line}"


def test_maybe_reconcile_payment_contains_no_daraja_call():
    source = inspect.getsource(payment_service.maybe_reconcile_payment)
    body = source.split('"""')[-1]
    assert "Daraja" not in body and "stk_" not in body
