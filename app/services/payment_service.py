"""Payment initiation and idempotent Daraja callback processing."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.daraja import DarajaClient, DarajaError, redact
from app.models.enums import OrderStatus, PaymentStatus, UserRole
from app.models.order import Order
from app.models.payment import Payment, PaymentEvent
from app.models.user import User


def callback_fields(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract the standard Daraja STK callback and metadata fields, tolerating malformed shapes."""
    body = payload.get("Body")
    callback = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(callback, dict):
        return {}, {}
    metadata = callback.get("CallbackMetadata")
    items = metadata.get("Item") if isinstance(metadata, dict) else None
    if not isinstance(items, list):
        items = []
    values = {str(item.get("Name")): item.get("Value") for item in items if isinstance(item, dict) and "Name" in item}
    return callback, values


async def handle_callback(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Persist every callback and apply a matching state transition at most once."""
    callback, metadata = callback_fields(payload)
    checkout_id = callback.get("CheckoutRequestID")
    if not isinstance(checkout_id, str):
        checkout_id = None
    initial_event = PaymentEvent(
        payment_id=None,
        checkout_request_id=checkout_id,
        event_type="callback",
        payload=redact(payload),
    )
    session.add(initial_event)
    await session.commit()
    if not checkout_id:
        return

    payment = await session.scalar(select(Payment).where(Payment.checkout_request_id == checkout_id))
    if payment is None:
        return
    await session.commit()
    initial_event.payment_id = payment.id
    await session.commit()

    result_code_value = callback.get("ResultCode")
    try:
        result_code = int(result_code_value)
    except (TypeError, ValueError):
        return

    await _apply_callback_result(session, payment, checkout_id, callback, metadata, result_code)


async def _apply_callback_result(
    session: AsyncSession,
    payment: Payment,
    checkout_id: str,
    callback: dict[str, Any],
    metadata: dict[str, Any],
    result_code: int,
) -> None:
    """Apply a validated callback under the payment's allowed state guard."""
    if result_code == 0 and not _amount_matches(metadata, payment.amount):
        session.add(
            PaymentEvent(
                payment_id=payment.id,
                checkout_request_id=checkout_id,
                event_type="error",
                payload={
                    "reason": "callback_amount_mismatch",
                    "expected": payment.amount,
                    "received": metadata.get("Amount"),
                },
            )
        )
        await session.commit()
        return

    async with session.begin():
        succeeded = result_code == 0
        if succeeded:
            new_status = PaymentStatus.COMPLETED
        elif result_code == 1032:
            new_status = PaymentStatus.CANCELLED
        else:
            new_status = PaymentStatus.FAILED
        eligible = Payment.status.in_([PaymentStatus.INITIATING, PaymentStatus.INITIATED])
        if succeeded:
            eligible = eligible | (Payment.status == PaymentStatus.TIMEOUT)
        values: dict[str, Any] = {
            "status": new_status,
            "result_code": result_code,
            "result_desc": callback.get("ResultDesc"),
            "callback_received_at": datetime.now(UTC),
        }
        if succeeded:
            values["mpesa_receipt_number"] = metadata.get("MpesaReceiptNumber")
            values["completed_at"] = datetime.now(UTC)
        result = await session.execute(update(Payment).where(Payment.id == payment.id, eligible).values(**values))
        if result.rowcount == 1 and succeeded:
            await _mark_order_paid(session, payment)


def _amount_matches(metadata: dict[str, Any], expected: int) -> bool:
    """Check callback amount without accepting missing or malformed values."""
    try:
        return Decimal(str(metadata["Amount"])) == Decimal(expected)
    except (KeyError, TypeError, InvalidOperation, ValueError):
        return False


async def _mark_order_paid(session: AsyncSession, payment: Payment) -> None:
    """Lock the related order and preserve reconciliation for expired orders."""
    from sqlalchemy import select

    order = await session.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
    if order is not None:
        if order.status == OrderStatus.EXPIRED:
            order.status = OrderStatus.NEEDS_RECONCILIATION
        else:
            order.status = OrderStatus.PAID


async def maybe_reconcile_payment(session: AsyncSession, order: Order) -> None:
    """Reconcile a stale open payment of ``order`` before it is shown; currently a deliberate no-op.

    ``GET /orders/{id}`` calls this once per request. Module 5 will implement it here: when the
    order's latest payment is ``initiated`` and older than ``RECONCILE_AFTER_SECONDS``, query the
    Daraja STK status and apply the result through callback processing. Until then it reads nothing
    and writes nothing, so order views never touch a payments row.
    """
    del session, order


async def reconcile_payment(session: AsyncSession, payment: Payment, client: DarajaClient) -> None:
    """Query a stale STK request and apply its result through callback processing."""
    if not payment.checkout_request_id:
        return
    response = await client.stk_query(payment.checkout_request_id)
    result_code = response.get("ResultCode")
    if result_code is None:
        return
    metadata: list[dict[str, Any]] = []
    if "MpesaReceiptNumber" in response:
        metadata.append({"Name": "MpesaReceiptNumber", "Value": response["MpesaReceiptNumber"]})
    if "Amount" in response:
        metadata.append({"Name": "Amount", "Value": response["Amount"]})
    await handle_callback(
        session,
        {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": payment.checkout_request_id,
                    "ResultCode": result_code,
                    "ResultDesc": response.get("ResultDesc", "Status query"),
                    "CallbackMetadata": {"Item": metadata},
                }
            }
        },
    )


async def initiate_payment(session: AsyncSession, order_id: UUID, buyer: User, idempotency_key: str) -> Payment:
    """Start an idempotent sandbox STK request using only persisted order/account values."""
    settings = get_settings()
    order = await session.get(Order, order_id)
    if order is None or order.buyer_id != buyer.id or order.buyer_role != UserRole.BUYER:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Order not found"})
    if order.status != OrderStatus.PENDING_PAYMENT or order.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=409, detail={"code": "order_not_payable", "message": "Order is not awaiting payment"}
        )

    existing = await session.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.order_id != order.id:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_conflict", "message": "Idempotency key was used for another order"},
            )
        return existing
    stk_phone = buyer.phone_number if settings.mpesa_stk_target == "registered" else settings.mpesa_test_msisdn
    payment = Payment(
        order_id=order.id,
        idempotency_key=idempotency_key,
        phone_number=buyer.phone_number,
        stk_phone_number=stk_phone,
        amount=order.total_amount,
        status=PaymentStatus.INITIATING,
        environment="sandbox",
    )
    session.add(payment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # payments_one_open_per_order_idx allows one initiating/initiated payment per order.
        if "payments_one_open_per_order_idx" in str(exc):
            raise HTTPException(
                status_code=409,
                detail={"code": "payment_in_progress", "message": "A payment for this order is already in progress"},
            ) from exc
        raise
    await session.refresh(payment)
    session.add(
        PaymentEvent(
            payment_id=payment.id,
            event_type="stk_request",
            payload=redact({"phone": stk_phone, "amount": payment.amount, "account_reference": order.reference}),
        )
    )
    await session.commit()

    callback_url = (
        f"{settings.mpesa_callback_base_url.rstrip('/')}/api/v1/payments/callback/{settings.mpesa_callback_token}"
    )
    client = DarajaClient(settings)
    try:
        response = await client.stk_push(stk_phone, payment.amount, order.reference, callback_url)
    except DarajaError as exc:
        payment.status = PaymentStatus.FAILED
        payment.result_desc = str(exc)
        session.add(PaymentEvent(payment_id=payment.id, event_type="error", payload={"message": str(exc)}))
    else:
        session.add(PaymentEvent(payment_id=payment.id, event_type="stk_response", payload=redact(response)))
        if response.get("ResponseCode") == "0":
            payment.checkout_request_id = response.get("CheckoutRequestID")
            payment.merchant_request_id = response.get("MerchantRequestID")
            payment.result_desc = response.get("ResponseDescription")
            payment.status = PaymentStatus.INITIATED
        else:
            payment.status = PaymentStatus.FAILED
            payment.result_desc = response.get("ResponseDescription", "Daraja rejected the STK request")
            session.add(
                PaymentEvent(payment_id=payment.id, event_type="error", payload={"message": payment.result_desc})
            )
    await session.commit()
    await session.refresh(payment)
    return payment
