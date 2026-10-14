"""Module 6 admin API tests without a database: roles, users, moderation, orders, payments, reports."""

import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from payment_fakes import NOW, FakeDB, FakeResult, make_order, make_payment
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_current_user
from app.core.errors import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.db import get_db
from app.models.enums import CropType, ListingStatus, OrderStatus, PaymentStatus, UserRole
from app.models.listing import ProduceListing
from app.models.payment import PaymentEvent
from app.models.user import User
from app.schemas import admin as admin_schemas
from app.schemas.admin import ResolveAction, ResolveOrderRequest
from app.schemas.common import Pagination
from app.services import (
    admin_listing_service,
    admin_order_service,
    admin_report_service,
    admin_user_service,
    listing_service,
    order_service,
)
from app.utils.search import contains_pattern, phone_prefix

APP_DIR = Path(__file__).resolve().parents[1] / "app"
GENERIC_CONFLICT = {"error": {"code": "conflict", "message": "An account with these details already exists"}}


def actor(role=UserRole.ADMIN, *, super_admin=False, must_change_password=False):
    return SimpleNamespace(
        id=uuid4(),
        name=f"Test {role.value}",
        role=role,
        is_super_admin=super_admin,
        is_disabled=False,
        must_change_password=must_change_password,
    )


def make_user(role=UserRole.FARMER, **overrides) -> User:
    values = {
        "id": uuid4(),
        "name": f"Test {role.value}",
        "phone_number": "254712345678",
        "email": f"{role.value}@example.com",
        "password_hash": hash_password("Original-passw0rd!"),
        "role": role,
        "is_super_admin": False,
        "must_change_password": False,
        "is_disabled": False,
        "disabled_at": None,
        "disabled_by": None,
        "created_by": None,
        "created_at": NOW,
    }
    values.update(overrides)
    return User(**values)


def user_row(user: User, listing_count=None, order_count=None, disabled_by_name=None, created_by_name=None):
    return FakeResult(rows=[(user, disabled_by_name, created_by_name, listing_count, order_count)])


def install(application, user, db: FakeDB | None = None):
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user
    session_db = db or FakeDB()

    async def request_session():
        yield session_db.session()

    application.dependency_overrides[get_db] = request_session
    return session_db


def lock_index(statements, table):
    return next(i for i, sql in enumerate(statements) if f"FROM {table}" in sql and "FOR UPDATE" in sql)


# Role matrix ---------------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("get", "/api/v1/admin/users", None, admin_user_service, "list_users"),
    ("patch", f"/api/v1/admin/users/{uuid4()}/disable", None, admin_user_service, "set_user_disabled"),
    ("patch", f"/api/v1/admin/users/{uuid4()}/enable", None, admin_user_service, "set_user_disabled"),
    ("post", f"/api/v1/admin/users/{uuid4()}/reset-password", None, admin_user_service, "reset_password"),
    ("get", "/api/v1/admin/listings", None, admin_listing_service, "list_listings"),
    ("delete", f"/api/v1/admin/listings/{uuid4()}", None, admin_listing_service, "remove_listing"),
    ("get", "/api/v1/admin/orders", None, admin_order_service, "list_orders"),
    ("get", f"/api/v1/admin/orders/{uuid4()}", None, admin_order_service, "get_order"),
    (
        "post",
        f"/api/v1/admin/orders/{uuid4()}/resolve",
        {"action": "void_order", "note": "Buyer confirmed by phone"},
        admin_order_service,
        "resolve_order",
    ),
    ("get", "/api/v1/admin/payments", None, admin_order_service, "list_payments"),
    ("get", f"/api/v1/admin/payments/{uuid4()}/events", None, admin_order_service, "payment_events"),
    ("get", "/api/v1/admin/stats", None, admin_report_service, "stats"),
    ("get", "/api/v1/admin/forecast-accuracy", None, admin_report_service, "forecast_accuracy"),
    (
        "post",
        "/api/v1/admin/admins",
        {"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co"},
        None,
        None,
    ),
]


async def call(api_client, method, path, body):
    if method in ("post", "patch") and body is not None:
        return await getattr(api_client, method)(path, json=body)
    return await getattr(api_client, method)(path)


@pytest.mark.parametrize(("method", "path", "body", "module", "name"), ADMIN_ROUTES)
async def test_every_admin_route_rejects_missing_tokens(api_client, application, method, path, body, module, name):
    install(application, None)
    response = await call(api_client, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER])
@pytest.mark.parametrize(("method", "path", "body", "module", "name"), ADMIN_ROUTES)
async def test_farmers_and_buyers_are_forbidden(api_client, application, role, method, path, body, module, name):
    install(application, actor(role))
    response = await call(api_client, method, path, body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(("method", "path", "body", "module", "name"), ADMIN_ROUTES)
async def test_password_change_guard_applies_to_admin_routes(api_client, application, method, path, body, module, name):
    install(application, None)
    application.dependency_overrides.pop(get_current_user, None)
    from app.core.dependencies import get_current_user_any

    application.dependency_overrides[get_current_user_any] = lambda: actor(must_change_password=True)
    response = await call(api_client, method, path, body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "password_change_required"


@pytest.mark.parametrize(("method", "path", "body", "module", "name"), ADMIN_ROUTES[:-1])
async def test_an_ordinary_admin_reaches_every_admin_route(
    api_client, application, monkeypatch, method, path, body, module, name
):
    reached = []

    async def stub(*args, **kwargs):
        reached.append(name)
        raise AppError(418, "reached", "service reached")

    monkeypatch.setattr(module, name, stub)
    install(application, actor())
    response = await call(api_client, method, path, body)
    assert response.status_code == 418, response.text
    assert reached == [name]


async def test_only_the_super_admin_creates_admins(api_client, application):
    install(application, actor())
    response = await api_client.post(
        "/api/v1/admin/admins", json={"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co"}
    )
    assert response.status_code == 403


# Create admin --------------------------------------------------------------------------------


def created_admin_row(db, created_by_name="Super Admin"):
    """The order view of the admin the service just added, with server defaults filled in."""
    admin = db.of_type(User)[-1]
    admin.created_at = NOW
    admin.is_disabled = False
    return FakeResult(rows=[(admin, None, created_by_name, None, None)])


async def test_create_admin_sets_role_flags_and_creator_on_the_server(api_client, application, caplog):
    caplog.set_level(logging.DEBUG)
    super_admin = actor(super_admin=True)
    db = install(application, super_admin, FakeDB(results=[created_admin_row]))
    response = await api_client.post(
        "/api/v1/admin/admins",
        json={"name": " New Admin ", "phone_number": "0712000001", "email": "New.Admin@Example.com"},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    created = db.of_type(User)[0]
    assert (created.role, created.is_super_admin, created.must_change_password) == (UserRole.ADMIN, False, True)
    assert created.created_by == super_admin.id
    assert (created.name, created.phone_number, created.email) == ("New Admin", "254712000001", "new.admin@example.com")
    temporary_password = body["temporary_password"]
    assert len(temporary_password) >= 16
    assert created.password_hash != temporary_password
    assert verify_password(created.password_hash, temporary_password)
    assert "password_hash" not in response.text and created.password_hash not in response.text
    assert body["role"] == "admin" and body["must_change_password"] is True
    assert body["created_by_name"] == "Super Admin"
    assert temporary_password not in caplog.text
    assert created.password_hash not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co", "role": "admin"},
        {"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co", "is_super_admin": True},
        {"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co", "temporary_password": "x" * 20},
        {"name": "New Admin", "phone_number": "0712000001"},
        {"name": "N", "phone_number": "0712000001", "email": "a@b.co"},
        {"name": "New Admin", "phone_number": "12345", "email": "a@b.co"},
    ],
)
async def test_create_admin_rejects_unknown_or_invalid_fields(api_client, application, body):
    db = install(application, actor(super_admin=True))
    response = await api_client.post("/api/v1/admin/admins", json=body)
    assert response.status_code == 422
    assert db.added == []


@pytest.mark.parametrize("constraint", ["users_phone_number_key", "users_email_lower_unique_idx"])
async def test_duplicate_phone_or_email_gives_the_same_generic_409(api_client, application, constraint):
    original = Exception(f'duplicate key value violates unique constraint "{constraint}"')
    original.sqlstate = "23505"
    error = IntegrityError("insert into users", {}, original)
    install(application, actor(super_admin=True), FakeDB(commit_errors=[error]))
    response = await api_client.post(
        "/api/v1/admin/admins", json={"name": "New Admin", "phone_number": "0712000001", "email": "a@b.co"}
    )
    assert response.status_code == 409
    assert response.json() == GENERIC_CONFLICT


async def test_a_new_admin_must_change_the_temporary_password(api_client, application):
    new_admin = make_user(UserRole.ADMIN, must_change_password=True)
    db = FakeDB(scalars=[new_admin])

    async def request_session():
        yield db.session()

    application.dependency_overrides[get_db] = request_session
    token = create_access_token(new_admin.id, new_admin.role)
    response = await api_client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "password_change_required"


# Disable, enable, reset ----------------------------------------------------------------------


def reread_from_database(target: User):
    """Like the real re-read after commit: the database has evaluated `disabled_at = now()`."""

    def result(db):
        del db
        if target.disabled_at is not None and not isinstance(target.disabled_at, type(NOW)):
            target.disabled_at = NOW
        return user_row(target)

    return result


async def disable(target, acting, disabled=True, extra_results=None):
    db = FakeDB(scalars=[target], results=extra_results or [reread_from_database(target)])
    out = await admin_user_service.set_user_disabled(db.session(), acting, target.id, disabled)
    return db, out


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER])
async def test_admin_disables_farmers_and_buyers(role):
    acting = actor()
    target = make_user(role)
    db, out = await disable(target, acting)
    assert target.is_disabled is True
    assert target.disabled_at is not None
    assert target.disabled_by == acting.id
    assert db.commits == 1
    assert "FOR UPDATE" in db.statements[0] and "FROM users" in db.statements[0]
    assert out.id == target.id


async def test_enable_clears_all_three_disable_fields():
    acting = actor()
    target = make_user(UserRole.BUYER, is_disabled=True, disabled_at=NOW, disabled_by=uuid4())
    await disable(target, acting, disabled=False)
    assert (target.is_disabled, target.disabled_at, target.disabled_by) == (False, None, None)


async def test_repeated_disable_is_idempotent():
    acting = actor()
    first_admin = uuid4()
    target = make_user(UserRole.FARMER, is_disabled=True, disabled_at=NOW, disabled_by=first_admin)
    db, _ = await disable(target, acting)
    assert (target.disabled_at, target.disabled_by) == (NOW, first_admin)
    assert db.commits == 1  # only to release the row lock


async def test_the_super_admin_cannot_be_disabled():
    target = make_user(UserRole.ADMIN, is_super_admin=True)
    with pytest.raises(AppError) as exc_info:
        await disable(target, actor(super_admin=True))
    assert (exc_info.value.status_code, exc_info.value.code) == (403, "cannot_modify_super_admin")
    assert target.is_disabled is False


async def test_an_admin_cannot_disable_themselves():
    acting = actor()
    target = make_user(UserRole.ADMIN, id=acting.id)
    with pytest.raises(AppError) as exc_info:
        await disable(target, acting)
    assert (exc_info.value.status_code, exc_info.value.code) == (403, "cannot_disable_self")


async def test_an_ordinary_admin_cannot_disable_another_admin():
    target = make_user(UserRole.ADMIN)
    with pytest.raises(AppError) as exc_info:
        await disable(target, actor())
    assert (exc_info.value.status_code, exc_info.value.code) == (403, "forbidden")
    assert target.is_disabled is False


async def test_the_super_admin_can_disable_an_admin():
    target = make_user(UserRole.ADMIN)
    await disable(target, actor(super_admin=True))
    assert target.is_disabled is True


async def test_unknown_user_is_404():
    db = FakeDB(scalars=[None])
    with pytest.raises(AppError) as exc_info:
        await admin_user_service.set_user_disabled(db.session(), actor(), uuid4(), True)
    assert (exc_info.value.status_code, exc_info.value.code) == (404, "user_not_found")


async def test_disable_route_rejects_a_body(api_client, application):
    db = install(application, actor())
    response = await api_client.patch(f"/api/v1/admin/users/{uuid4()}/disable", json={"is_disabled": True})
    assert response.status_code == 422
    assert db.statements == []


async def test_a_disabled_users_next_request_is_401(api_client, application):
    disabled = make_user(UserRole.BUYER, is_disabled=True, disabled_at=NOW)
    db = FakeDB(scalars=[disabled])

    async def request_session():
        yield db.session()

    application.dependency_overrides[get_db] = request_session
    token = create_access_token(disabled.id, disabled.role)
    response = await api_client.get("/api/v1/orders/mine", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_reset_password_replaces_the_hash_and_forces_a_change(api_client, application, caplog):
    caplog.set_level(logging.DEBUG)
    target = make_user(UserRole.FARMER)
    install(application, actor(), FakeDB(scalars=[target]))
    response = await api_client.post(f"/api/v1/admin/users/{target.id}/reset-password")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    temporary_password = response.json()["temporary_password"]
    assert verify_password(target.password_hash, temporary_password)
    assert not verify_password(target.password_hash, "Original-passw0rd!")
    assert target.must_change_password is True
    assert temporary_password not in caplog.text
    assert set(response.json()) == {"temporary_password"}


async def test_reset_password_refuses_the_super_admin():
    target = make_user(UserRole.ADMIN, is_super_admin=True)
    with pytest.raises(AppError) as exc_info:
        await admin_user_service.reset_password(FakeDB(scalars=[target]).session(), actor(super_admin=True), target.id)
    assert exc_info.value.code == "cannot_modify_super_admin"


async def test_an_ordinary_admin_cannot_reset_an_admin():
    target = make_user(UserRole.ADMIN)
    original_hash = target.password_hash
    with pytest.raises(AppError) as exc_info:
        await admin_user_service.reset_password(FakeDB(scalars=[target]).session(), actor(), target.id)
    assert (exc_info.value.status_code, exc_info.value.code) == (403, "forbidden")
    assert target.password_hash == original_hash


# Users list ----------------------------------------------------------------------------------


async def test_users_list_uses_one_count_and_one_page_statement():
    farmer = make_user(UserRole.FARMER)
    buyer = make_user(UserRole.BUYER)
    admin = make_user(UserRole.ADMIN)
    rows = [(farmer, None, None, 3, None), (buyer, None, "Super", None, 2), (admin, None, None, None, None)]
    db = FakeDB(scalars=[3], results=[FakeResult(rows=rows)])
    page = await admin_user_service.list_users(db.session(), Pagination(), role=None, is_disabled=False, q="50%_off")
    assert len(db.statements) == 2
    counts = [(item.role, item.listing_count, item.order_count) for item in page.items]
    assert counts == [("farmer", 3, None), ("buyer", None, 2), ("admin", None, None)]
    page_sql = db.statements[1]
    assert "ILIKE" in page_sql and "ESCAPE" in page_sql
    assert "count(*)" in page_sql.lower() and "GROUP BY" in page_sql
    assert "password_hash" not in json.dumps([item.model_dump(mode="json") for item in page.items])


def test_search_wildcards_are_matched_literally():
    assert contains_pattern("50%_off") == r"%50\%\_off%"
    assert contains_pattern("a\\b") == r"%a\\b%"
    assert phone_prefix("0712 34") == "25471234"
    assert phone_prefix("John") is None


@pytest.mark.parametrize("q", ["a", "x" * 51])
async def test_search_length_is_validated(api_client, application, q):
    install(application, actor())
    response = await api_client.get("/api/v1/admin/users", params={"q": q})
    assert response.status_code == 422


# Listing moderation --------------------------------------------------------------------------


def listing(**overrides) -> ProduceListing:
    values = {
        "id": uuid4(),
        "farmer_id": uuid4(),
        "farmer_role": UserRole.FARMER,
        "crop": CropType.MAIZE,
        "market": "Nairobi",
        "quantity_available": Decimal("4.00"),
        "unit": "kg",
        "price_per_unit": Decimal("40.00"),
        "image_path": None,
        "status": ListingStatus.ACTIVE,
        "removed_at": None,
        "removed_by": None,
        "created_at": NOW,
    }
    values.update(overrides)
    return ProduceListing(**values)


async def test_removal_cancels_unpaid_orders_and_restores_their_stock():
    acting = actor()
    item = listing(quantity_available=Decimal("0.00"), status=ListingStatus.SOLD_OUT)
    orders = [
        make_order(uuid4(), listing_id=item.id, quantity=Decimal("2.50")),
        make_order(uuid4(), listing_id=item.id, quantity=Decimal("1.50")),
    ]
    db = FakeDB(lists=[orders], scalars=[None, item])
    await admin_listing_service.remove_listing(db.session(), acting, item.id)
    assert [order.status for order in orders] == [OrderStatus.CANCELLED, OrderStatus.CANCELLED]
    assert item.quantity_available == Decimal("4.00")
    assert item.status == ListingStatus.REMOVED
    assert item.removed_at is not None and item.removed_by == acting.id
    assert lock_index(db.statements, "orders") < lock_index(db.statements, "produce_listings")
    assert "ORDER BY orders.id" in db.statements[0]


async def test_an_open_payment_blocks_removal_and_changes_nothing():
    item = listing()
    order = make_order(uuid4(), listing_id=item.id)
    db = FakeDB(lists=[[order]], scalars=[uuid4()])
    with pytest.raises(AppError) as exc_info:
        await admin_listing_service.remove_listing(db.session(), actor(), item.id)
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "listing_has_open_payments")
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert item.status == ListingStatus.ACTIVE
    assert db.commits == 0


async def test_repeated_removal_returns_204(api_client, application):
    removed = listing(status=ListingStatus.REMOVED, removed_at=NOW)
    db = install(application, actor(), FakeDB(lists=[[]], scalars=[removed]))
    response = await api_client.delete(f"/api/v1/admin/listings/{removed.id}")
    assert response.status_code == 204
    assert removed.removed_at == NOW
    assert db.commits == 1


async def test_removing_an_unknown_listing_is_404():
    with pytest.raises(AppError) as exc_info:
        await admin_listing_service.remove_listing(FakeDB(lists=[[]], scalars=[None]).session(), actor(), uuid4())
    assert exc_info.value.code == "listing_not_found"


async def test_admin_listings_include_counts_from_one_page_query():
    item = listing()
    db = FakeDB(scalars=[1], results=[FakeResult(rows=[(item, "Farmer Jane", True, None, 2, 1)])])
    page = await admin_listing_service.list_listings(db.session(), Pagination(), q="Jane")
    assert len(db.statements) == 2
    out = page.items[0]
    assert (out.farmer_name, out.farmer_disabled, out.pending_orders, out.paid_orders) == ("Farmer Jane", True, 2, 1)
    assert "GROUP BY orders.listing_id" in db.statements[1]


# Orders and payments -------------------------------------------------------------------------


def order_row(**overrides):
    row = {
        "id": uuid4(),
        "reference": "K7QX2MPA",
        "status": "paid",
        "buyer_name": "Buyer Ann",
        "seller_name": "Farmer Jane",
        "listing_id": uuid4(),
        "crop": "maize",
        "market": "Nairobi",
        "quantity": Decimal("2.50"),
        "unit_price": Decimal("45.50"),
        "total_amount": 114,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "latest_payment_status": "completed",
        "latest_mpesa_receipt_number": "SBX1",
        "rc_reason": "late_success_after_expiry",
        "rc_order_status_before": "expired",
        "rc_flagged_at": NOW,
        "rs_action": None,
        "rs_note": None,
        "rs_admin_name": None,
        "rs_created_at": None,
    }
    row.update(overrides)
    return row


async def test_reconciliation_appears_only_for_flagged_orders():
    rows = [order_row(status="needs_reconciliation"), order_row(status="paid")]
    db = FakeDB(scalars=[2], results=[FakeResult(mapping_rows=rows)])
    page = await admin_order_service.list_orders(db.session(), Pagination(), status_filter=None, q="k7q")
    flagged, paid = page.items
    assert flagged.reconciliation is not None
    assert (flagged.reconciliation.reason, flagged.reconciliation.order_status_before) == (
        "late_success_after_expiry",
        "expired",
    )
    assert paid.reconciliation is None
    sql = db.statements[1]
    assert "->> 'order_status_before'" in sql and " ? " not in sql
    assert "order by e.created_at desc" in sql
    assert len(db.statements) == 2


async def test_order_detail_never_exposes_payment_secrets(api_client, application):
    order = make_order(uuid4())
    payment = make_payment(order, idempotency_key="secret-idempotency-key", merchant_request_id="merchant-xyz")
    install(
        application,
        actor(),
        FakeDB(results=[FakeResult(mapping_rows=[order_row(id=order.id)])], lists=[[payment]]),
    )
    response = await api_client.get(f"/api/v1/admin/orders/{order.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payments"][0]["stk_target_masked"] == "254*******678"
    for forbidden in ("secret-idempotency-key", "merchant-xyz", "254712345678", "idempotency", "merchant"):
        assert forbidden not in response.text


async def test_unknown_order_is_404():
    with pytest.raises(AppError) as exc_info:
        await admin_order_service.get_order(FakeDB(results=[FakeResult()]).session(), uuid4())
    assert exc_info.value.code == "order_not_found"


async def test_payments_list_masks_targets_and_numbers_attempts():
    row = {
        "id": uuid4(),
        "order_id": uuid4(),
        "order_reference": "K7QX2MPA",
        "order_status": "paid",
        "buyer_name": "Buyer Ann",
        "amount": 114,
        "status": "completed",
        "stk_phone_number": "254708374149",
        "mpesa_receipt_number": "SBX1",
        "result_code": 0,
        "result_desc": "ok",
        "environment": "sandbox",
        "created_at": NOW,
        "completed_at": NOW,
        "callback_received_at": NOW,
        "attempt_number": 2,
    }
    db = FakeDB(scalars=[1], results=[FakeResult(mapping_rows=[row])])
    page = await admin_order_service.list_payments(
        db.session(), Pagination(), status_filter=PaymentStatus.COMPLETED, created_from=date(2026, 10, 1)
    )
    item = page.items[0]
    assert (item.stk_target_masked, item.attempt_number) == ("254*******149", 2)
    dumped = json.dumps(item.model_dump(mode="json"))
    assert "254708374149" not in dumped and "idempotency" not in dumped and "merchant" not in dumped
    assert "Africa/Nairobi" in db.statements[1]


async def test_event_timeline_is_ordered_and_capped():
    payment_id = uuid4()
    event = PaymentEvent(
        id=uuid4(), payment_id=payment_id, checkout_request_id="ws_CO_1", event_type="callback", payload={"a": 1}
    )
    event.created_at = NOW
    db = FakeDB(scalars=[payment_id], lists=[[event]])
    timeline = await admin_order_service.payment_events(db.session(), payment_id)
    assert [item.event_type for item in timeline.items] == ["callback"]
    sql = db.statements[1]
    assert "ORDER BY payment_events.created_at ASC" in sql and "LIMIT" in sql


async def test_unknown_payment_events_is_404():
    with pytest.raises(AppError) as exc_info:
        await admin_order_service.payment_events(FakeDB(scalars=[None]).session(), uuid4())
    assert exc_info.value.code == "payment_not_found"


# Resolve -------------------------------------------------------------------------------------


def flagged(before, *, listing_kwargs=None, quantity="2.00"):
    order = make_order(uuid4(), status=OrderStatus.NEEDS_RECONCILIATION, quantity=Decimal(quantity))
    item = listing(id=order.listing_id, **(listing_kwargs or {}))
    payment = make_payment(order, status=PaymentStatus.COMPLETED, mpesa_receipt_number="SBX1")
    return order, item, payment, before


async def resolve(order, item, payment, before, action, note="Confirmed with the buyer by phone"):
    acting = actor()
    db = FakeDB(
        scalars=[order, item],
        results=[
            FakeResult(rows=[("late_success_after_expiry", before)] if before else []),
            FakeResult(mapping_rows=[order_row(id=order.id, status="paid")]),
        ],
        lists=[[payment], [payment]],
    )
    out = await admin_order_service.resolve_order(
        db.session(), acting, order.id, ResolveOrderRequest(action=action, note=note)
    )
    return db, acting, out


async def test_confirm_on_a_pending_payment_context_marks_paid_without_touching_stock():
    order, item, payment, before = flagged("pending_payment")
    db, acting, _ = await resolve(order, item, payment, before, ResolveAction.CONFIRM_SALE)
    assert order.status == OrderStatus.PAID
    assert item.quantity_available == Decimal("4.00")
    event = db.events_of("admin_action")[0]
    assert event.payload == {
        "action": "confirm_sale",
        "note": "Confirmed with the buyer by phone",
        "admin_id": str(acting.id),
        "order_id": str(order.id),
        "previous_status": "needs_reconciliation",
        "order_status_before": "pending_payment",
        "stock_change": "0",
    }
    assert event.payment_id == payment.id


async def test_confirm_after_expiry_re_reserves_stock():
    order, item, payment, before = flagged("expired", listing_kwargs={"quantity_available": Decimal("2.00")})
    db, _, _ = await resolve(order, item, payment, before, ResolveAction.CONFIRM_SALE)
    assert order.status == OrderStatus.PAID
    assert item.quantity_available == Decimal("0.00")
    assert item.status == ListingStatus.SOLD_OUT
    assert db.events_of("admin_action")[0].payload["stock_change"] == "-2.00"


async def test_confirm_with_insufficient_stock_is_409_and_changes_nothing():
    order, item, payment, before = flagged("expired", listing_kwargs={"quantity_available": Decimal("1.00")})
    with pytest.raises(AppError) as exc_info:
        await resolve(order, item, payment, before, ResolveAction.CONFIRM_SALE)
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "insufficient_stock_to_confirm")
    assert order.status == OrderStatus.NEEDS_RECONCILIATION
    assert item.quantity_available == Decimal("1.00")


async def test_void_on_a_pending_payment_context_restores_stock():
    order, item, payment, before = flagged(
        "pending_payment", listing_kwargs={"quantity_available": Decimal("0.00"), "status": ListingStatus.SOLD_OUT}
    )
    db, _, _ = await resolve(order, item, payment, before, ResolveAction.VOID_ORDER)
    assert order.status == OrderStatus.CANCELLED
    assert item.quantity_available == Decimal("2.00")
    assert item.status == ListingStatus.ACTIVE
    assert db.events_of("admin_action")[0].payload["stock_change"] == "2.00"


async def test_void_on_an_expired_context_does_not_touch_stock():
    order, item, payment, before = flagged("expired")
    await resolve(order, item, payment, before, ResolveAction.VOID_ORDER)
    assert order.status == OrderStatus.CANCELLED
    assert item.quantity_available == Decimal("4.00")


@pytest.mark.parametrize(
    ("before", "action"), [("expired", ResolveAction.CONFIRM_SALE), ("pending_payment", ResolveAction.VOID_ORDER)]
)
async def test_a_removed_listing_is_never_touched(before, action):
    order, item, payment, _ = flagged(
        before, listing_kwargs={"status": ListingStatus.REMOVED, "removed_at": NOW, "quantity_available": Decimal("1")}
    )
    db, _, _ = await resolve(order, item, payment, before, action)
    assert item.quantity_available == Decimal("1")
    assert item.status == ListingStatus.REMOVED
    assert db.events_of("admin_action")[0].payload["stock_change"] == "0"


async def test_a_missing_context_is_409():
    order, item, payment, _ = flagged(None)
    with pytest.raises(AppError) as exc_info:
        await resolve(order, item, payment, None, ResolveAction.CONFIRM_SALE)
    assert exc_info.value.code == "reconciliation_context_missing"
    assert order.status == OrderStatus.NEEDS_RECONCILIATION


async def test_a_second_resolve_is_409():
    order = make_order(uuid4(), status=OrderStatus.PAID)
    db = FakeDB(scalars=[order])
    with pytest.raises(AppError) as exc_info:
        await admin_order_service.resolve_order(
            db.session(), actor(), order.id, ResolveOrderRequest(action="confirm_sale", note="Second attempt here")
        )
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "order_not_in_reconciliation")


async def test_unknown_order_resolve_is_404():
    with pytest.raises(AppError) as exc_info:
        await admin_order_service.resolve_order(
            FakeDB(scalars=[None]).session(), actor(), uuid4(), ResolveOrderRequest(action="void_order", note="x" * 10)
        )
    assert exc_info.value.code == "order_not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"action": "void_order", "note": "too short"},
        {"action": "refund", "note": "Buyer confirmed by phone"},
        {"action": "void_order", "note": "x" * 501},
        {"action": "void_order", "note": "Buyer confirmed by phone", "amount": 100},
    ],
)
async def test_invalid_resolve_bodies_are_422(api_client, application, body):
    db = install(application, actor())
    response = await api_client.post(f"/api/v1/admin/orders/{uuid4()}/resolve", json=body)
    assert response.status_code == 422
    assert db.statements == []


async def test_resolution_is_one_transaction_with_the_documented_lock_order():
    order, item, payment, before = flagged("expired")
    receipt_before = (payment.status, payment.mpesa_receipt_number, payment.amount)
    db, _, _ = await resolve(order, item, payment, before, ResolveAction.CONFIRM_SALE)
    event = db.events_of("admin_action")[0]
    adding_session = next(session for session, row in db.added_by if row is event)
    assert adding_session is db.sessions[0]
    assert len(db.sessions) == 1
    statements = db.statements
    assert lock_index(statements, "orders") < lock_index(statements, "payments")
    assert lock_index(statements, "payments") < lock_index(statements, "produce_listings")
    assert (payment.status, payment.mpesa_receipt_number, payment.amount) == receipt_before


def test_resolve_note_minimum_follows_the_setting(monkeypatch):
    with pytest.raises(ValidationError):
        ResolveOrderRequest(action="void_order", note="  nine   ")
    assert ResolveOrderRequest(action="void_order", note="  ten chars!  ").note == "ten chars!"


# Statistics ----------------------------------------------------------------------------------


async def test_stats_fill_every_status_and_flag_stale_forecasts():
    dashboard = [
        ("users_by_role", "farmer", Decimal(3)),
        ("users_by_role", "buyer", Decimal(5)),
        ("users_disabled", "disabled", Decimal(1)),
        ("orders_by_status", "paid", Decimal(2)),
        ("orders_by_status", "needs_reconciliation", Decimal(1)),
        ("payments_by_status", "completed", Decimal(2)),
        ("completed_sandbox_amount", "sandbox", Decimal(228)),
    ]
    attention = {"payments_initiated_over_10_minutes": 1, "last_prices_fetch": NOW, "last_forecast_generated": NOW}
    series = [
        {
            "market": "Nairobi",
            "crop": "maize",
            "latest_price_month": date(2026, 9, 1),
            "forecast_origin_month": date(2026, 8, 1),
        },
        {
            "market": "Nairobi",
            "crop": "beans",
            "latest_price_month": date(2026, 9, 1),
            "forecast_origin_month": date(2026, 9, 1),
        },
    ]
    db = FakeDB(
        results=[FakeResult(rows=dashboard), FakeResult(mapping_rows=[attention]), FakeResult(mapping_rows=series)]
    )
    stats = await admin_report_service.stats(db.session())
    assert set(stats.orders) == {status.value for status in OrderStatus}
    assert stats.orders["expired"] == 0 and stats.orders["paid"] == 2
    assert set(stats.listings) == {status.value for status in ListingStatus}
    assert set(stats.payments) == {status.value for status in PaymentStatus}
    assert (stats.users.farmers, stats.users.buyers, stats.users.admins, stats.users.disabled) == (3, 5, 0, 1)
    assert stats.completed_sandbox_amount_kes == 228
    assert stats.attention.orders_needing_reconciliation == 1
    assert [(s.crop, s.forecast_current) for s in stats.pipeline.series] == [("maize", False), ("beans", True)]
    assert len(db.statements) == 3
    assert "dashboard_stats" in db.statements[0]


# Forecast accuracy ---------------------------------------------------------------------------


def accuracy_row(**overrides):
    row = {
        "market": "Nairobi",
        "crop": "maize",
        "horizon": "one_month",
        "origin_month": date(2026, 8, 1),
        "target_month": date(2026, 9, 1),
        "origin_price": Decimal("50.00"),
        "predicted_price": Decimal("52.00"),
        "actual_price": None,
        "abs_error": None,
        "pct_error": None,
        "actual_log_change": None,
        "direction_correct": None,
        "persistence_abs_error": None,
        "beat_persistence": None,
        "is_backfill": False,
        "is_pending": True,
    }
    row.update(overrides)
    return row


async def test_accuracy_keeps_pending_rows_null_and_summarises_live_and_backfill_separately():
    summary = [
        {
            "horizon": "one_month",
            "is_backfill": False,
            "n_realised": 0,
            "n_pending": 1,
            "mape": None,
            "mean_abs_error": None,
            "directional_accuracy": None,
            "beat_persistence_rate": None,
        },
        {
            "horizon": "one_month",
            "is_backfill": True,
            "n_realised": 4,
            "n_pending": 0,
            "mape": Decimal("6.25"),
            "mean_abs_error": Decimal("3.10"),
            "directional_accuracy": Decimal("0.75"),
            "beat_persistence_rate": Decimal("0.50"),
        },
    ]
    db = FakeDB(
        scalars=[1],
        results=[FakeResult(mapping_rows=[accuracy_row()]), FakeResult(mapping_rows=summary)],
    )
    out = await admin_report_service.forecast_accuracy(
        db.session(), Pagination(), market="Nairobi", crop=CropType.MAIZE, is_backfill=None
    )
    payload = json.loads(out.model_dump_json())
    assert payload["items"][0]["pct_error"] is None and payload["items"][0]["is_pending"] is True
    assert [(row["is_backfill"], row["mape"]) for row in payload["summary"]] == [(False, None), (True, 6.25)]
    summary_sql = db.statements[2]
    assert "avg(abs(pct_error)) filter (where not is_pending)" in summary_sql
    assert "group by horizon, is_backfill" in summary_sql
    assert "lower(market) = lower(" in db.statements[1]
    assert "crop = cast(" in db.statements[1]
    assert "Backfilled" in out.note


async def test_accuracy_can_exclude_pending_rows():
    db = FakeDB(scalars=[0], results=[FakeResult(mapping_rows=[]), FakeResult(mapping_rows=[])])
    await admin_report_service.forecast_accuracy(db.session(), Pagination(), include_pending=False)
    assert "not is_pending" in db.statements[0] and "not is_pending" in db.statements[1]
    assert "where not is_pending" not in db.statements[2].split("from public.forecast_accuracy")[1]


# Earlier modules: disabled farmers -----------------------------------------------------------


async def test_browse_hides_listings_of_disabled_farmers():
    from app.schemas.listing import ListingBrowseFilters

    db = FakeDB(scalars=[0], results=[FakeResult(rows=[])])
    await listing_service.browse_listings(db.session(), ListingBrowseFilters(), Pagination())
    for sql in db.statements:
        assert "NOT (EXISTS" in sql and "is_disabled IS true" in sql


async def test_detail_of_a_disabled_farmers_listing_is_404():
    db = FakeDB(results=[FakeResult()])
    with pytest.raises(AppError) as exc_info:
        await listing_service.get_public_listing(db.session(), uuid4())
    assert exc_info.value.code == "listing_not_found"
    assert "users.is_disabled IS false" in db.statements[0]


async def test_checkout_on_a_disabled_farmers_listing_is_404():
    db = FakeDB(scalars=[None])
    buyer = SimpleNamespace(id=uuid4(), role=UserRole.BUYER)
    with pytest.raises(AppError) as exc_info:
        await order_service.create_order(db.session(), buyer, uuid4(), Decimal("1"))
    assert (exc_info.value.status_code, exc_info.value.code) == (404, "listing_not_found")
    assert "NOT (EXISTS" in db.statements[0] and "FOR UPDATE OF produce_listings" in db.statements[0]


# Source checks -------------------------------------------------------------------------------

ADMIN_SERVICES = [
    "services/admin_user_service.py",
    "services/admin_listing_service.py",
    "services/admin_order_service.py",
    "services/admin_report_service.py",
]
ADMIN_FILES = [
    *ADMIN_SERVICES,
    "routers/admin.py",
    "routers/admin_users.py",
    "routers/admin_listings.py",
    "routers/admin_orders.py",
    "routers/admin_reports.py",
    "schemas/admin.py",
]


@pytest.mark.parametrize("name", ADMIN_SERVICES)
def test_admin_services_never_use_float(name):
    assert re.search(r"\bfloat\b", (APP_DIR / name).read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("name", ADMIN_FILES)
def test_admin_files_import_no_ml_or_daraja_code(name):
    source = (APP_DIR / name).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines() if line.strip().startswith(("import ", "from "))]
    for line in imports:
        for banned in ("sklearn", "xgboost", "shap", "daraja"):
            assert banned not in line.lower(), f"{name}: {line}"


def test_no_response_model_has_a_password_hash():
    for value in vars(admin_schemas).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            assert "password_hash" not in value.model_fields, value.__name__


def test_no_admin_route_returns_payment_secrets(application):
    schemas = json.dumps(application.openapi()["components"]["schemas"])
    for forbidden in ("password_hash", "idempotency_key", "merchant_request_id"):
        assert forbidden not in schemas
