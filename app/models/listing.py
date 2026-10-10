"""Produce listing table mapping."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.models.enums import CropType, ListingStatus, UserRole, crop_type_type, listing_status_type, user_role_type
from app.models.market import MarketLocation


class ProduceListing(Base):
    __tablename__ = "produce_listings"
    __table_args__ = (ForeignKeyConstraint(["farmer_id", "farmer_role"], ["users.id", "users.role"]),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farmer_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    farmer_role: Mapped[UserRole] = mapped_column(user_role_type, nullable=False, server_default=text("'farmer'"))
    crop: Mapped[CropType] = mapped_column(crop_type_type, nullable=False)
    # Reference the mapped column rather than a "table.column" string so market_locations is
    # always registered in the metadata when this model is used; otherwise flushes fail.
    market: Mapped[str] = mapped_column(ForeignKey(MarketLocation.market_name), nullable=False)
    quantity_available: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'kg'"))
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    image_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ListingStatus] = mapped_column(listing_status_type, nullable=False, server_default=text("'active'"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
