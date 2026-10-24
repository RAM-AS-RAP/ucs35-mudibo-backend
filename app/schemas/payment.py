"""Payment request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

# Checked for length only here; the service normalises it and answers 422 invalid_phone when it is not
# a valid Safaricom number, so the message is the same for every bad format.
EnteredPhone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=20)]


class PayRequest(BaseModel):
    """Optional body of ``POST /orders/{id}/pay``: the M-Pesa number to send the prompt to."""

    model_config = ConfigDict(extra="forbid")

    phone_number: EnteredPhone | None = None


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
