"""Administrator request and response schemas. No output schema here carries a password hash."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.config import get_settings
from app.schemas.common import DecimalOutput, Page

SEARCH_MIN_LENGTH = 2
SEARCH_MAX_LENGTH = 50
RESOLVE_NOTE_MAX_LENGTH = 500

SearchQuery = Annotated[str | None, Query(min_length=SEARCH_MIN_LENGTH, max_length=SEARCH_MAX_LENGTH)]


# Users ---------------------------------------------------------------------------------------


class UserAdminOut(BaseModel):
    id: UUID
    name: str
    phone_number: str
    email: str | None
    role: str
    is_super_admin: bool
    is_disabled: bool
    disabled_at: datetime | None
    disabled_by_name: str | None
    must_change_password: bool
    created_at: datetime
    created_by_name: str | None
    listing_count: int | None
    order_count: int | None


class UserAdminPage(Page[UserAdminOut]):
    pass


# Listings ------------------------------------------------------------------------------------


class AdminListingOut(DecimalOutput):
    id: UUID
    farmer_id: UUID
    farmer_name: str
    farmer_disabled: bool
    crop: str
    market: str
    quantity_available: Decimal
    unit: str
    price_per_unit: Decimal
    image_url: str | None
    status: str
    removed_at: datetime | None
    removed_by_name: str | None
    pending_orders: int
    paid_orders: int
    created_at: datetime


class AdminListingPage(Page[AdminListingOut]):
    pass


# Orders and payments -------------------------------------------------------------------------


class ReconciliationOut(BaseModel):
    """Why an order was flagged, from the latest error event carrying ``order_status_before``."""

    reason: str | None
    order_status_before: str
    flagged_at: datetime


class ResolutionOut(BaseModel):
    """The latest ``admin_action`` event recorded for the order."""

    action: str | None
    note: str | None
    admin_name: str | None
    created_at: datetime


class AdminOrderOut(DecimalOutput):
    id: UUID
    reference: str
    status: str
    buyer_name: str
    seller_name: str
    listing_id: UUID
    crop: str
    market: str
    quantity: Decimal
    unit_price: Decimal
    total_amount: int
    created_at: datetime
    expires_at: datetime
    latest_payment_status: str | None
    latest_mpesa_receipt_number: str | None
    reconciliation: ReconciliationOut | None = None
    resolution: ResolutionOut | None = None


class AdminOrderPage(Page[AdminOrderOut]):
    pass


class AdminOrderPaymentOut(BaseModel):
    id: UUID
    status: str
    amount: int
    stk_target_masked: str
    mpesa_receipt_number: str | None
    result_code: int | None
    result_desc: str | None
    environment: str
    created_at: datetime
    completed_at: datetime | None
    callback_received_at: datetime | None


class AdminOrderDetail(AdminOrderOut):
    payments: list[AdminOrderPaymentOut]


class ResolveAction(StrEnum):
    CONFIRM_SALE = "confirm_sale"
    VOID_ORDER = "void_order"


class ResolveOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ResolveAction
    note: str = Field(max_length=RESOLVE_NOTE_MAX_LENGTH)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        stripped = value.strip()
        minimum = get_settings().resolve_note_min_length
        if len(stripped) < minimum:
            raise ValueError(f"Note must contain at least {minimum} characters")
        return stripped


class AdminPaymentOut(BaseModel):
    id: UUID
    order_id: UUID
    order_reference: str
    order_status: str
    buyer_name: str
    amount: int
    status: str
    stk_target_masked: str
    mpesa_receipt_number: str | None
    result_code: int | None
    result_desc: str | None
    environment: str
    created_at: datetime
    completed_at: datetime | None
    callback_received_at: datetime | None
    attempt_number: int


class AdminPaymentPage(Page[AdminPaymentOut]):
    pass


class PaymentEventOut(BaseModel):
    id: UUID
    event_type: str
    checkout_request_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class PaymentEventList(BaseModel):
    payment_id: UUID
    items: list[PaymentEventOut]
    limit: int


# Statistics ----------------------------------------------------------------------------------


class UserCounts(BaseModel):
    farmers: int
    buyers: int
    admins: int
    disabled: int


class AttentionCounts(BaseModel):
    orders_needing_reconciliation: int
    payments_initiated_over_10_minutes: int


class PipelineSeries(BaseModel):
    market: str
    crop: str
    latest_price_month: date | None
    forecast_origin_month: date | None
    forecast_current: bool


class PipelineStatus(BaseModel):
    series: list[PipelineSeries]
    last_prices_fetch: datetime | None
    last_forecast_generated: datetime | None


class StatsOut(BaseModel):
    users: UserCounts
    listings: dict[str, int]
    orders: dict[str, int]
    payments: dict[str, int]
    completed_sandbox_amount_kes: int
    attention: AttentionCounts
    pipeline: PipelineStatus


# Forecast accuracy ---------------------------------------------------------------------------

_TWO_PLACES = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")


def _rounded(value: Decimal | None, places: Decimal) -> object:
    """JSON number rounded half up; computed in Decimal and converted only for serialization."""
    if value is None:
        return None
    # float-ok: display serializer; money stays Decimal and becomes a JSON number only here.
    return float(value.quantize(places, rounding=ROUND_HALF_UP))


class AccuracyItemOut(BaseModel):
    market: str
    crop: str
    horizon: str
    origin_month: date
    target_month: date
    origin_price: Decimal
    predicted_price: Decimal
    actual_price: Decimal | None
    abs_error: Decimal | None
    pct_error: Decimal | None
    actual_log_change: Decimal | None
    direction_correct: bool | None
    persistence_abs_error: Decimal | None
    beat_persistence: bool | None
    is_backfill: bool
    is_pending: bool

    @field_serializer(
        "origin_price", "predicted_price", "actual_price", "abs_error", "pct_error", "persistence_abs_error"
    )
    def serialize_two_places(self, value: Decimal | None) -> object:
        return _rounded(value, _TWO_PLACES)

    @field_serializer("actual_log_change")
    def serialize_log_change(self, value: Decimal | None) -> object:
        return _rounded(value, _FOUR_PLACES)


class AccuracySummaryOut(DecimalOutput):
    horizon: str
    is_backfill: bool
    n_realised: int
    n_pending: int
    mape: Decimal | None
    mean_abs_error: Decimal | None
    directional_accuracy: Decimal | None
    beat_persistence_rate: Decimal | None


class AccuracyOut(BaseModel):
    items: list[AccuracyItemOut]
    total: int
    limit: int
    offset: int
    summary: list[AccuracySummaryOut]
    note: str
