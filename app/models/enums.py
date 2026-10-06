"""Python values for PostgreSQL enum types owned by SQL migrations."""

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class UserRole(StrEnum):
    FARMER = "farmer"
    BUYER = "buyer"
    ADMIN = "admin"


class CropType(StrEnum):
    MAIZE = "maize"
    BEANS = "beans"


class ForecastHorizon(StrEnum):
    ONE_MONTH = "one_month"
    TWO_MONTH = "two_month"


class PriceSource(StrEnum):
    FEWS = "fews"
    WFP = "wfp"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    SOLD_OUT = "sold_out"
    REMOVED = "removed"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class PaymentStatus(StrEnum):
    INITIATING = "initiating"
    INITIATED = "initiated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def pg_enum(enum_class: type[StrEnum], name: str) -> ENUM:
    """Reference an existing PostgreSQL enum without creating it."""
    return ENUM(
        enum_class,
        name=name,
        create_type=False,
        values_callable=lambda values: [item.value for item in values],
    )


user_role_type = pg_enum(UserRole, "user_role")
crop_type_type = pg_enum(CropType, "crop_type")
forecast_horizon_type = pg_enum(ForecastHorizon, "forecast_horizon")
price_source_type = pg_enum(PriceSource, "price_source")
listing_status_type = pg_enum(ListingStatus, "listing_status")
order_status_type = pg_enum(OrderStatus, "order_status")
payment_status_type = pg_enum(PaymentStatus, "payment_status")
