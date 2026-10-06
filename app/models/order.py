"""Marketplace order table mapping."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.models.enums import OrderStatus, UserRole, order_status_type, user_role_type


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (ForeignKeyConstraint(["buyer_id", "buyer_role"], ["users.id", "users.role"]),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    reference: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    listing_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("produce_listings.id"), nullable=False)
    buyer_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    buyer_role: Mapped[UserRole] = mapped_column(user_role_type, nullable=False, server_default=text("'buyer'"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        order_status_type, nullable=False, server_default=text("'pending_payment'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
