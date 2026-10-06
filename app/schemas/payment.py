"""Payment request and response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: UUID
    amount: int
    status: str
    checkout_request_id: str | None
    mpesa_receipt_number: str | None
    result_desc: str | None
    created_at: datetime
