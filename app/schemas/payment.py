"""Payment request and response schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Checked for length only here; the service answers 422 phone_required when the number is missing or
# empty and 422 invalid_phone when it is not a valid Safaricom number, so the message is the same for
# every bad format.
EnteredPhone = Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)]


class PayRequest(BaseModel):
    """Body of ``POST /orders/{id}/pay``: the M-Pesa number to send the prompt to (required).

    Accounts have no phone number, so this is the only source of the number. A missing body, a missing
    field or an empty value is answered with 422 ``phone_required`` by the service (not by validation),
    which is why the field is optional to the validator but required in the published schema.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"required": ["phone_number"]})

    phone_number: EnteredPhone | None = Field(
        default=None, description="The M-Pesa number for this payment, for example 0712 345 678."
    )


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
