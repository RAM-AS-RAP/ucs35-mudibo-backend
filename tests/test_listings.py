"""Mocked farmer listing routes and services without database or network access."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.integrations.storage import StorageError
from app.models.enums import CropType, ListingStatus, UserRole
from app.models.listing import ProduceListing
from app.schemas.common import Pagination
from app.schemas.listing import ListingOut
from app.services import listing_service, order_service


async def fake_db():
    yield object()


def make_user(role: UserRole = UserRole.FARMER, **kwargs):
    return SimpleNamespace(
        id=kwargs.pop("id", uuid4()), role=role, is_disabled=False, must_change_password=False, **kwargs
    )


def make_listing(owner_id=None, **kwargs):
    return ProduceListing(
        id=kwargs.pop("id", uuid4()),
        farmer_id=owner_id or uuid4(),
        farmer_role=UserRole.FARMER,
        crop=kwargs.pop("crop", CropType.MAIZE),
        market=kwargs.pop("market", "Nairobi"),
        quantity_available=kwargs.pop("quantity_available", Decimal("10.00")),
        unit=kwargs.pop("unit", "kg"),
        price_per_unit=kwargs.pop("price_per_unit", Decimal("50.00")),
        image_path=kwargs.pop("image_path", None),
        status=kwargs.pop("status", ListingStatus.ACTIVE),
        removed_at=kwargs.pop("removed_at", None),
        removed_by=kwargs.pop("removed_by", None),
        created_at=kwargs.pop("created_at", datetime.now(UTC)),
        updated_at=kwargs.pop("updated_at", datetime.now(UTC)),
    )


class ListingSession:
    def __init__(self, scalar_results=(), commit_error=None):
        self.scalar_results = list(scalar_results)
        self.commit_error = commit_error
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed = []

    async def scalar(self, statement):
        del statement
        return self.scalar_results.pop(0)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, row):
        self.refreshed.append(row)
        if getattr(row, "created_at", None) is None:
            row.created_at = datetime.now(UTC)
        if getattr(row, "updated_at", None) is None:
            row.updated_at = datetime.now(UTC)

    async def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params
        return self.result


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Decimal("1.00")),
        ("1.2", Decimal("1.20")),
        ("1.235", None),
        ("NaN", None),
        ("100001", None),
        ("0", None),
        ("-1", None),
    ],
)
def test_decimal_listing_input_policy(raw, expected):
    if expected is None:
        with pytest.raises(AppError):
            listing_service.parse_decimal_input(raw, "price_per_unit")
    else:
        assert listing_service.parse_decimal_input(raw, "price_per_unit") == expected


def test_zero_quantity_is_allowed_only_for_edits():
    assert listing_service.parse_decimal_input("0", "quantity_available", allow_zero=True) == Decimal("0.00")
    with pytest.raises(AppError):
        listing_service.parse_decimal_input("0", "quantity_available")


def test_listing_money_serializes_as_rounded_json_numbers():
    response = ListingOut(
        id=uuid4(),
        farmer_id=uuid4(),
        crop="beans",
        market="Kisumu",
        quantity_available=Decimal("2.675"),
        unit="kg",
        price_per_unit=Decimal("12.345"),
        image_url=None,
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    payload = json.loads(response.model_dump_json())
    assert response.price_per_unit == Decimal("12.345")
    assert payload["quantity_available"] == 2.68
    assert payload["price_per_unit"] == 12.35


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "expected_code"),
    [
        (
            {"crop": "rice", "market": "Nairobi", "quantity_available": "1", "unit": "kg", "price_per_unit": "1"},
            "validation_error",
        ),
        (
            {"crop": "maize", "market": "Mombasa", "quantity_available": "1", "unit": "kg", "price_per_unit": "1"},
            "market_not_available",
        ),
        (
            {"crop": "beans", "market": "Nairobi", "quantity_available": "1", "unit": "bag", "price_per_unit": "1"},
            "validation_error",
        ),
        (
            {"crop": "beans", "market": "Nairobi", "quantity_available": "0", "unit": "kg", "price_per_unit": "1"},
            "validation_error",
        ),
    ],
)
async def test_listing_create_rejects_invalid_values(monkeypatch, values, expected_code):
    async def require_market(session, market):
        if market == "Mombasa":
            raise AppError(404, "market_not_available", "Market is not available.")
        return market

    monkeypatch.setattr(listing_service, "require_available_market", require_market)
    with pytest.raises(AppError) as exc_info:
        await listing_service._validate_listing_values(ListingSession(), values, allow_zero=False)
    assert exc_info.value.code == expected_code


def test_form_schemas_reject_unknown_fields():
    from pydantic import ValidationError

    from app.schemas.listing import ListingCreateForm, ListingEditForm

    with pytest.raises(ValidationError):
        ListingCreateForm.model_validate(
            {
                "crop": "maize",
                "market": "Nairobi",
                "quantity_available": "1",
                "price_per_unit": "10",
                "farmer_id": str(uuid4()),
            }
        )
    for immutable_field, value in (("crop", "beans"), ("market", "Mombasa"), ("unit", "bag")):
        with pytest.raises(ValidationError):
            ListingEditForm.model_validate({immutable_field: value})


@pytest.mark.asyncio
async def test_create_listing_uses_authenticated_farmer_and_returns_public_image(monkeypatch):
    farmer = make_user()
    session = ListingSession(scalar_results=[0])
    monkeypatch.setattr(listing_service, "require_available_market", _canonical_market)
    monkeypatch.setattr(listing_service, "get_settings", lambda: SimpleNamespace(max_active_listings_per_farmer=50))
    monkeypatch.setattr(listing_service, "upload_image", _record_upload)
    monkeypatch.setattr(listing_service, "public_url", lambda path: f"https://storage.test/{path}")
    monkeypatch.setattr("app.utils.images.process_image", _processed_image)
    upload = object()
    out = await listing_service.create_listing(
        session,
        farmer,
        {"crop": " MAIZE ", "market": "Nairobi", "quantity_available": "3.50", "unit": "kg", "price_per_unit": "14.25"},
        upload,
    )
    listing = session.added[0]
    assert listing.farmer_id == farmer.id
    assert listing.farmer_role == UserRole.FARMER
    assert listing.crop == CropType.MAIZE
    assert listing.status == ListingStatus.ACTIVE
    assert out.image_url.endswith(".jpg")
    assert out.quantity_available == Decimal("3.50")
    assert session.commits == 1


@pytest.mark.asyncio
async def test_create_listing_limit_is_enforced_before_insert(monkeypatch):
    session = ListingSession(scalar_results=[50])
    monkeypatch.setattr(listing_service, "get_settings", lambda: SimpleNamespace(max_active_listings_per_farmer=50))
    with pytest.raises(AppError) as exc_info:
        await listing_service.create_listing(session, make_user(), {}, None)
    assert exc_info.value.code == "listing_limit_reached"
    assert not session.added


@pytest.mark.asyncio
async def test_insert_failure_deletes_uploaded_image(monkeypatch):
    farmer = make_user()
    session = ListingSession(scalar_results=[0], commit_error=RuntimeError("insert failed"))
    deleted = []
    monkeypatch.setattr(listing_service, "require_available_market", _canonical_market)
    monkeypatch.setattr(listing_service, "get_settings", lambda: SimpleNamespace(max_active_listings_per_farmer=50))
    monkeypatch.setattr(listing_service, "upload_image", _record_upload)
    monkeypatch.setattr(listing_service, "delete_image", _record_delete(deleted))
    monkeypatch.setattr("app.utils.images.process_image", _processed_image)
    with pytest.raises(RuntimeError, match="insert failed"):
        await listing_service.create_listing(
            session,
            farmer,
            {"crop": "beans", "market": "Kisumu", "quantity_available": "1", "unit": "kg", "price_per_unit": "9"},
            object(),
        )
    assert len(deleted) == 1
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_upload_failure_does_not_insert_listing(monkeypatch):
    session = ListingSession(scalar_results=[0])
    monkeypatch.setattr(listing_service, "require_available_market", _canonical_market)
    monkeypatch.setattr(listing_service, "get_settings", lambda: SimpleNamespace(max_active_listings_per_farmer=50))
    monkeypatch.setattr("app.utils.images.process_image", _processed_image)

    async def fail_upload(path, content):
        raise StorageError("upload rejected")

    monkeypatch.setattr(listing_service, "upload_image", fail_upload)
    with pytest.raises(StorageError):
        await listing_service.create_listing(
            session,
            make_user(),
            {"crop": "maize", "market": "Nairobi", "quantity_available": "1", "unit": "kg", "price_per_unit": "2"},
            object(),
        )
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_listing_add_failure_cleans_uploaded_object(monkeypatch):
    session = ListingSession(scalar_results=[0])
    deleted = []
    monkeypatch.setattr(listing_service, "require_available_market", _canonical_market)
    monkeypatch.setattr(listing_service, "get_settings", lambda: SimpleNamespace(max_active_listings_per_farmer=50))
    monkeypatch.setattr("app.utils.images.process_image", _processed_image)
    monkeypatch.setattr(listing_service, "upload_image", _record_upload)
    monkeypatch.setattr(listing_service, "delete_image", _record_delete(deleted))

    def fail_add(row):
        raise RuntimeError("listing insert failed")

    monkeypatch.setattr(session, "add", fail_add)
    with pytest.raises(RuntimeError, match="listing insert failed"):
        await listing_service.create_listing(
            session,
            make_user(),
            {"crop": "maize", "market": "Nairobi", "quantity_available": "1", "unit": "kg", "price_per_unit": "2"},
            object(),
        )
    assert session.added == []
    assert len(deleted) == 1
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_update_hides_foreign_listing_as_not_found():
    session = ListingSession(scalar_results=[None])
    with pytest.raises(AppError) as exc_info:
        await listing_service.update_listing(session, make_user(), uuid4(), {"quantity_available": "2"})
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "listing_not_found"


@pytest.mark.asyncio
async def test_delete_missing_and_foreign_listing_have_identical_404():
    farmer = make_user()
    errors = []
    for session in (ListingSession([None]), ListingSession([None])):
        with pytest.raises(AppError) as exc_info:
            await listing_service.remove_listing(session, farmer, uuid4())
        errors.append((exc_info.value.status_code, exc_info.value.code, exc_info.value.message))
    assert errors[0] == errors[1] == (404, "listing_not_found", "Listing not found.")


@pytest.mark.asyncio
async def test_update_quantity_transitions_sold_out_listing_to_active():
    farmer = make_user()
    listing = make_listing(farmer.id, status=ListingStatus.SOLD_OUT, quantity_available=Decimal("0.00"))
    session = ListingSession(scalar_results=[listing])
    out = await listing_service.update_listing(session, farmer, listing.id, {"quantity_available": "2.25"})
    assert listing.quantity_available == Decimal("2.25")
    assert listing.status == ListingStatus.ACTIVE
    assert out.status == ListingStatus.ACTIVE
    assert session.commits == 1


@pytest.mark.asyncio
async def test_old_image_is_deleted_only_after_update_commit(monkeypatch):
    farmer = make_user()
    listing = make_listing(farmer.id, image_path="listings/old/image.jpg")
    session = ListingSession(scalar_results=[listing])
    deleted = []
    monkeypatch.setattr("app.utils.images.process_image", _processed_image)
    monkeypatch.setattr(listing_service, "upload_image", _record_upload)

    async def delete_after_commit(path):
        deleted.append((path, session.commits))
        return True

    monkeypatch.setattr(listing_service, "delete_image", delete_after_commit)
    await listing_service.update_listing(session, farmer, listing.id, {}, object())
    assert deleted == [("listings/old/image.jpg", 1)]


@pytest.mark.asyncio
async def test_removed_listing_cannot_be_edited():
    farmer = make_user()
    listing = make_listing(farmer.id, status=ListingStatus.REMOVED, removed_at=datetime.now(UTC))
    with pytest.raises(AppError) as exc_info:
        await listing_service.update_listing(ListingSession([listing]), farmer, listing.id, {"price_per_unit": "20"})
    assert exc_info.value.code == "listing_removed"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_no_changes_returns_specific_validation_error():
    with pytest.raises(AppError) as exc_info:
        await listing_service.update_listing(ListingSession(), make_user(), uuid4(), {})
    assert exc_info.value.code == "no_changes"


@pytest.mark.asyncio
async def test_remove_listing_blocked_by_pending_order():
    farmer = make_user()
    listing = make_listing(farmer.id)
    session = ListingSession([listing, uuid4()])
    with pytest.raises(AppError) as exc_info:
        await listing_service.remove_listing(session, farmer, listing.id)
    assert exc_info.value.code == "listing_has_pending_orders"
    assert listing.status == ListingStatus.ACTIVE
    assert session.commits == 0


@pytest.mark.asyncio
async def test_remove_listing_is_idempotent():
    farmer = make_user()
    listing = make_listing(farmer.id, status=ListingStatus.REMOVED, removed_at=datetime.now(UTC))
    session = ListingSession([listing])
    assert await listing_service.remove_listing(session, farmer, listing.id) is None
    assert session.commits == 0


@pytest.mark.asyncio
async def test_remove_listing_sets_soft_delete_fields_when_no_pending_order():
    farmer = make_user()
    listing = make_listing(farmer.id)
    session = ListingSession([listing, None])
    await listing_service.remove_listing(session, farmer, listing.id)
    assert listing.status == ListingStatus.REMOVED
    assert listing.removed_by == farmer.id
    assert listing.removed_at is not None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_farmer_order_projection_hides_buyer_contact_and_ids():
    farmer = make_user()
    session = ListingSession()
    session.result = _Rows(
        [
            {
                "order_id": uuid4(),
                "reference": "ABC12345",
                "listing_id": uuid4(),
                "crop": "maize",
                "market": "Nairobi",
                "quantity": Decimal("2"),
                "unit_price": Decimal("20"),
                "total_amount": 40,
                "status": "paid",
                "created_at": datetime.now(UTC),
                "buyer_name": "Buyer",
                "payment_status": "completed",
                "payment_environment": "sandbox",
                "mpesa_receipt_number": "RCP",
                "payment_completed_at": datetime.now(UTC),
                "total": 1,
            }
        ]
    )
    page = await order_service.list_orders_for_farmer(session, farmer, Pagination(limit=20, offset=0))
    output = page.model_dump_json()
    assert page.total == 1
    assert page.note
    for secret_field in (
        "buyer_id",
        "buyer_phone",
        "buyer_email",
        "idempotency_key",
        "checkout_request_id",
        "result_desc",
    ):
        assert secret_field not in output
    assert "Buyer" in output
    assert "RCP" in output


async def _canonical_market(session, market):
    return market


async def _processed_image(upload):
    del upload
    return b"processed-jpeg"


async def _record_upload(path, content):
    assert path.startswith("listings/") and path.endswith(".jpg")
    assert content == b"processed-jpeg"


def _record_delete(paths):
    async def delete(path):
        paths.append(path)
        return True

    return delete


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows
