"""Daraja sandbox payments: starting STK requests, applying results, callbacks, and reconciliation.

Lock order, shared with ``public.expire_stale_orders()`` and the order service: orders, then
payments, then listings. Payment code locks an order and then its payment, and never locks a listing.

No database transaction is held open while an HTTP request to Daraja is in flight. Payment events
are written by ``log_event`` in their own short transactions, so they survive a rollback of the
caller's transaction.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.integrations.daraja import (
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    STATE_PENDING,
    DarajaClient,
    DarajaError,
    DarajaTimeout,
    StkPushResult,
    outcome_for_result_code,
    parse_result_code,
    redact,
)
from app.models.enums import OrderStatus, PaymentStatus, UserRole
from app.models.order import Order
from app.models.payment import Payment, PaymentEvent
from app.models.user import User
from app.utils.phone import mask_phone

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

EVENT_PAYLOAD_MAX_BYTES = 16384
RAW_BODY_PREVIEW_BYTES = 1024
OPEN_STATUSES = (PaymentStatus.INITIATING, PaymentStatus.INITIATED)
TERMINAL_STATUSES = (PaymentStatus.COMPLETED, PaymentStatus.FAILED, PaymentStatus.CANCELLED)
CLOSED_ORDER_STATUSES = (OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.PAYMENT_FAILED)

APPLIED = "applied"
DUPLICATE = "duplicate"
MISMATCH = "mismatch"
UNMATCHED = "unmatched"

SOURCE_CALLBACK = "callback"
SOURCE_QUERY = "query"

REQUEST_NOT_SENT = "request_not_sent"
REQUEST_REJECTED = "request_rejected"
REQUEST_UNCONFIRMED = "request_unconfirmed"
AMOUNT_MISMATCH = "amount_mismatch"

# Reasons recorded in the error event written when an order becomes needs_reconciliation.
RECONCILE_LATE_SUCCESS_AFTER_EXPIRY = "late_success_after_expiry"
RECONCILE_SUCCESS_ON_CLOSED_PAYMENT = "success_on_closed_payment"
RECONCILE_SUCCESS_ON_CLOSED_ORDER = "success_on_closed_order"

PROMPT_MESSAGE = "Check your phone and enter your M-Pesa PIN."
PAYMENT_FAILED_MESSAGE = "Could not start the M-Pesa prompt. Please try again."
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")

_OUTCOME_STATUS = {
    OUTCOME_CANCELLED: PaymentStatus.CANCELLED,
    OUTCOME_TIMEOUT: PaymentStatus.TIMEOUT,
    OUTCOME_FAILED: PaymentStatus.FAILED,
}
_LONG_DIGITS = re.compile(r"\d{9,}")


# Configuration -------------------------------------------------------------------------------


def payments_unavailable() -> AppError:
    return AppError(
        status.HTTP_503_SERVICE_UNAVAILABLE, "payments_unavailable", "Payments are temporarily unavailable."
    )


def require_payments_configured(settings: Settings, client: DarajaClient | None) -> None:
    """Fail with 503 when the payment settings are missing, the callback URL is not HTTPS, or no client exists."""
    if client is None or not settings.payments_configured:
        raise payments_unavailable()


def callback_url(settings: Settings) -> str:
    """The URL Daraja calls back. It contains the secret token, so it is never logged or stored."""
    return f"{settings.mpesa_callback_base_url.rstrip('/')}/api/v1/payments/callback/{settings.mpesa_callback_token}"


def stk_target(settings: Settings, buyer: User) -> str:
    """The phone that receives the prompt: the buyer's number, or the sandbox test number."""
    return settings.mpesa_test_msisdn if settings.mpesa_stk_target == "test_msisdn" else buyer.phone_number


# Event logging -------------------------------------------------------------------------------


def _mask_long_digits(text: str) -> str:
    """Mask any run of 9 or more digits (a possible phone number) in free text."""
    return _LONG_DIGITS.sub(lambda match: mask_phone(match.group()), text)


def _json_safe(payload: Any) -> Any:
    """Convert Decimals, datetimes and UUIDs to JSON values, never through a binary floating point type."""
    return json.loads(json.dumps(payload, default=str))


def _capped(payload: Any) -> dict[str, Any]:
    """Redact the payload and cap its JSON size at 16 KB, replacing an oversize body with a truncated preview."""
    safe = _json_safe(redact(payload))
    if not isinstance(safe, dict):
        safe = {"value": safe}
    text = json.dumps(safe)
    size = len(text.encode())
    if size <= EVENT_PAYLOAD_MAX_BYTES:
        return safe
    keep = EVENT_PAYLOAD_MAX_BYTES - 256
    while True:
        preview = _mask_long_digits(text.encode()[:keep].decode("utf-8", "ignore"))
        capped = {"truncated": True, "original_bytes": size, "preview": preview}
        overflow = len(json.dumps(capped).encode()) - EVENT_PAYLOAD_MAX_BYTES
        if overflow <= 0:
            return capped
        # Escaping (quotes, backslashes) makes the stored preview longer than the bytes kept.
        keep -= overflow


async def log_event(
    session_factory: SessionFactory,
    event_type: str,
    payload: Any,
    payment_id: UUID | None = None,
    checkout_request_id: str | None = None,
) -> UUID | None:
    """Append one payment event in its own short transaction and return its id, or None if it was not saved.

    The payload is redacted (no passwords, tokens or full phone numbers) and capped at 16 KB. Logging never
    raises: an event that cannot be stored is reported in the application log by exception type only.
    """
    event_id = uuid4()
    try:
        async with session_factory() as session:
            session.add(
                PaymentEvent(
                    id=event_id,
                    payment_id=payment_id,
                    checkout_request_id=checkout_request_id,
                    event_type=event_type,
                    payload=_capped(payload),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - an event must never break the payment flow
        logger.warning("Could not store a %s payment event: %s", event_type, type(exc).__name__)
        return None
    return event_id


# Applying results ----------------------------------------------------------------------------


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _flag_for_reconciliation(
    session: AsyncSession, order: Order, payment: Payment, reason: str, checkout_request_id: str
) -> None:
    """Set the order to ``needs_reconciliation`` and record why, in the caller's transaction.

    The ``error`` event is added to the same session, so it commits or rolls back together with the
    status change. Its ``order_status_before`` is the context the admin resolve endpoint reads.
    """
    order_status_before = str(order.status)
    order.status = OrderStatus.NEEDS_RECONCILIATION
    session.add(
        PaymentEvent(
            id=uuid4(),
            payment_id=payment.id,
            checkout_request_id=checkout_request_id,
            event_type="error",
            payload={
                "reason": reason,
                "order_status_before": order_status_before,
                "checkout_request_id": checkout_request_id,
            },
        )
    )


def _apply_success_to_order(session: AsyncSession, order: Order, payment: Payment, checkout_request_id: str) -> None:
    """A confirmed payment marks a pending order paid; a closed order needs an administrator."""
    if order.status == OrderStatus.PENDING_PAYMENT:
        order.status = OrderStatus.PAID
    elif order.status in CLOSED_ORDER_STATUSES:
        # The stock of an expired or cancelled order was already restored, so it cannot simply be paid.
        reason = (
            RECONCILE_LATE_SUCCESS_AFTER_EXPIRY
            if order.status == OrderStatus.EXPIRED
            else RECONCILE_SUCCESS_ON_CLOSED_ORDER
        )
        _flag_for_reconciliation(session, order, payment, reason, checkout_request_id)


async def _lock_order_then_payment(
    session: AsyncSession, order_id: UUID, payment_id: UUID
) -> tuple[Order | None, Payment | None]:
    """Lock the order first and the payment second, and re-read both after the locks are held."""
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True)
    )
    return order, payment


async def apply_payment_result(
    session_factory: SessionFactory,
    checkout_request_id: str,
    outcome: str,
    *,
    result_code: int | None,
    result_desc: str | None,
    receipt: str | None = None,
    amount: object = None,
    source: str,
) -> str:
    """Apply a Daraja result to a payment and its order once; return applied, duplicate, mismatch or unmatched.

    Safe to call repeatedly and concurrently for the same checkout id: the order and payment rows are
    locked (order first) and the payment state is re-read under the lock before anything changes.
    """
    error: dict[str, Any] | None = None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Payment.id, Payment.order_id).where(Payment.checkout_request_id == checkout_request_id)
            )
        ).first()
        if row is None:
            return UNMATCHED
        payment_id, order_id = row
        order, payment = await _lock_order_then_payment(session, order_id, payment_id)
        if order is None or payment is None:
            await session.rollback()
            return UNMATCHED

        result = DUPLICATE
        changed = False
        if payment.status in TERMINAL_STATUSES:
            if (
                outcome == OUTCOME_SUCCESS
                and payment.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED)
                and order.status not in (OrderStatus.PAID, OrderStatus.NEEDS_RECONCILIATION)
            ):
                _flag_for_reconciliation(
                    session, order, payment, RECONCILE_SUCCESS_ON_CLOSED_PAYMENT, checkout_request_id
                )
        elif payment.status in (PaymentStatus.INITIATED, PaymentStatus.TIMEOUT) and outcome == OUTCOME_SUCCESS:
            if amount is not None and _as_decimal(amount) != Decimal(payment.amount):
                # Never mark anything paid when the confirmed amount differs from what was requested.
                if payment.status == PaymentStatus.INITIATED:
                    payment.status = PaymentStatus.FAILED
                    payment.result_code = result_code
                    payment.result_desc = AMOUNT_MISMATCH
                    changed = True
                if order.status not in (OrderStatus.PAID, OrderStatus.NEEDS_RECONCILIATION):
                    _flag_for_reconciliation(session, order, payment, AMOUNT_MISMATCH, checkout_request_id)
                else:
                    error = {"reason": AMOUNT_MISMATCH, "expected": payment.amount, "received": str(amount)}
                result = MISMATCH
            else:
                payment.status = PaymentStatus.COMPLETED
                payment.result_code = result_code
                payment.result_desc = result_desc
                payment.mpesa_receipt_number = receipt
                payment.completed_at = func.now()
                _apply_success_to_order(session, order, payment, checkout_request_id)
                changed = True
                result = APPLIED
        elif payment.status == PaymentStatus.INITIATED and outcome in _OUTCOME_STATUS:
            # The order stays payable until it expires; stock is not touched.
            payment.status = _OUTCOME_STATUS[outcome]
            payment.result_code = result_code
            payment.result_desc = result_desc
            changed = True
            result = APPLIED
        # Anything else (a non-success result for a timed-out payment, or an initiating payment) is a duplicate.

        if changed and source == SOURCE_CALLBACK:
            payment.callback_received_at = func.now()
        try:
            await session.commit()
        except IntegrityError:
            # For example a receipt number that already belongs to another payment.
            await session.rollback()
            error = {"reason": "result_not_applied", "detail": "integrity_error"}
            result = DUPLICATE

    if error is not None:
        await log_event(
            session_factory,
            "error",
            {**error, "outcome": outcome, "source": source, "result_code": result_code},
            payment_id,
            checkout_request_id,
        )
    return result


# Callbacks -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedCallback:
    checkout_request_id: str
    merchant_request_id: str | None
    result_code: int
    result_desc: str | None
    amount: object
    receipt: str | None

    @property
    def outcome(self) -> str:
        return outcome_for_result_code(self.result_code)


def parse_callback(payload: Any) -> ParsedCallback | None:
    """Extract the STK callback fields, matching metadata items by Name; the phone number is ignored."""
    if not isinstance(payload, dict):
        return None
    body = payload.get("Body")
    callback = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(callback, dict):
        return None
    checkout_request_id = callback.get("CheckoutRequestID")
    result_code = parse_result_code(callback.get("ResultCode"))
    if not isinstance(checkout_request_id, str) or not checkout_request_id or result_code is None:
        return None
    metadata = callback.get("CallbackMetadata")
    items = metadata.get("Item") if isinstance(metadata, dict) else None
    values: dict[str, Any] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("Name") in ("Amount", "MpesaReceiptNumber"):
            values[item["Name"]] = item.get("Value")
    merchant_request_id = callback.get("MerchantRequestID")
    result_desc = callback.get("ResultDesc")
    receipt = values.get("MpesaReceiptNumber")
    return ParsedCallback(
        checkout_request_id=checkout_request_id,
        merchant_request_id=merchant_request_id if isinstance(merchant_request_id, str) else None,
        result_code=result_code,
        result_desc=str(result_desc)[:255] if result_desc is not None else None,
        amount=values.get("Amount"),
        receipt=str(receipt) if receipt not in (None, "") else None,
    )


async def _apply_callback(session_factory: SessionFactory, parsed: ParsedCallback) -> str:
    receipt = parsed.receipt if parsed.outcome == OUTCOME_SUCCESS else None
    amount = parsed.amount if parsed.outcome == OUTCOME_SUCCESS else None
    if parsed.outcome == OUTCOME_SUCCESS and amount is None:
        # A success callback must state the amount; a missing one is treated like a mismatch.
        amount = "missing"
    return await apply_payment_result(
        session_factory,
        parsed.checkout_request_id,
        parsed.outcome,
        result_code=parsed.result_code,
        result_desc=parsed.result_desc,
        receipt=receipt,
        amount=amount,
        source=SOURCE_CALLBACK,
    )


async def process_callback(session_factory: SessionFactory, raw_body: bytes) -> str | None:
    """Record a Daraja callback and apply it. Never raises for a malformed or unknown callback."""
    try:
        payload = json.loads(raw_body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        payload = None
    parsed = parse_callback(payload)
    if parsed is None:
        preview = _mask_long_digits(raw_body[:RAW_BODY_PREVIEW_BYTES].decode("utf-8", "replace"))
        reason = "malformed_json" if payload is None else "unexpected_callback_shape"
        await log_event(session_factory, "error", {"reason": reason, "body": preview})
        return None

    event_id = await log_event(session_factory, "callback", payload, checkout_request_id=parsed.checkout_request_id)
    async with session_factory() as session:
        payment_id = await session.scalar(
            select(Payment.id).where(Payment.checkout_request_id == parsed.checkout_request_id)
        )
        if payment_id is not None and event_id is not None:
            await session.execute(
                update(PaymentEvent)
                .where(PaymentEvent.id == event_id, PaymentEvent.payment_id.is_(None))
                .values(payment_id=payment_id)
            )
            await session.commit()
    if payment_id is None:
        # Kept as an orphan event; rematch_orphan_events applies it once the payment has its checkout id.
        return UNMATCHED
    return await _apply_callback(session_factory, parsed)


async def rematch_orphan_events(session_factory: SessionFactory, checkout_request_id: str) -> str | None:
    """Attach callbacks that arrived before the checkout id was saved, and apply the newest one.

    The callback route may attach the same event concurrently, so the update only touches rows whose
    ``payment_id`` is still null (re-checked by Postgres under the row lock). Rows already attached by
    the callback route are skipped here, and that route applies their result itself.
    """
    async with session_factory() as session:
        payment_id = await session.scalar(select(Payment.id).where(Payment.checkout_request_id == checkout_request_id))
        if payment_id is None:
            return None
        attached = (
            await session.execute(
                update(PaymentEvent)
                .where(
                    PaymentEvent.checkout_request_id == checkout_request_id,
                    PaymentEvent.event_type == "callback",
                    PaymentEvent.payment_id.is_(None),
                )
                .values(payment_id=payment_id)  # the only update payment_events allows: null to a value
                .returning(PaymentEvent.created_at, PaymentEvent.id, PaymentEvent.payload)
            )
        ).all()
        await session.commit()
    if not attached:
        return None
    newest = max(attached, key=lambda row: (row[0], str(row[1])))[2]
    parsed = parse_callback(newest)
    if parsed is None:
        return None
    return await _apply_callback(session_factory, parsed)


# Reconciliation ------------------------------------------------------------------------------


def _viewer_owns(viewer: User, order: Order) -> bool:
    return str(getattr(viewer.role, "value", viewer.role)) == UserRole.BUYER and viewer.id == order.buyer_id


async def _fail_stale_initiating(session_factory: SessionFactory, order_id: UUID, payment_id: UUID) -> None:
    """Fail an ``initiating`` payment whose STK request was never sent (order locked before payment)."""
    settings = get_settings()
    async with session_factory() as session:
        order, payment = await _lock_order_then_payment(session, order_id, payment_id)
        db_now = await session.scalar(select(func.now()))
        if (
            order is not None
            and payment is not None
            and payment.status == PaymentStatus.INITIATING
            and payment.checkout_request_id is None
            and db_now - payment.created_at >= timedelta(seconds=settings.mpesa_stale_initiating_seconds)
        ):
            payment.status = PaymentStatus.FAILED
            payment.result_desc = REQUEST_NOT_SENT
        await session.commit()


async def _reconcile(session_factory: SessionFactory, order_id: UUID, client: DarajaClient | None) -> None:
    settings = get_settings()
    async with session_factory() as session:
        latest = (
            await session.execute(
                select(Payment.id, Payment.status, Payment.checkout_request_id, Payment.created_at, func.now())
                .where(Payment.order_id == order_id)
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .limit(1)
            )
        ).first()
    if latest is None:
        return
    payment_id, payment_status, checkout_request_id, created_at, db_now = latest
    age = db_now - created_at

    if payment_status == PaymentStatus.INITIATING:
        if checkout_request_id is None and age >= timedelta(seconds=settings.mpesa_stale_initiating_seconds):
            await _fail_stale_initiating(session_factory, order_id, payment_id)
        return
    if checkout_request_id is None:
        return
    due = (
        payment_status == PaymentStatus.INITIATED and age >= timedelta(seconds=settings.reconcile_after_seconds)
    ) or (
        payment_status == PaymentStatus.TIMEOUT
        and age < timedelta(minutes=settings.mpesa_timeout_reconcile_window_minutes)
    )
    if not due:
        return

    await rematch_orphan_events(session_factory, checkout_request_id)
    async with session_factory() as session:
        current = await session.scalar(select(Payment.status).where(Payment.id == payment_id))
        since_last_query = await session.scalar(
            select(func.now() - func.max(PaymentEvent.created_at)).where(
                PaymentEvent.checkout_request_id == checkout_request_id,
                PaymentEvent.event_type == "status_query",
            )
        )
    if current not in (PaymentStatus.INITIATED, PaymentStatus.TIMEOUT):
        return
    if since_last_query is not None and since_last_query < timedelta(seconds=settings.mpesa_query_min_interval_seconds):
        return
    if client is None:
        return

    try:
        query = await client.stk_query(checkout_request_id)
    except DarajaError as exc:
        # Recorded as a status_query too, so the throttle also spaces out retries while Daraja is failing.
        await log_event(
            session_factory,
            "status_query",
            {"state": "request_failed", "error": type(exc).__name__},
            payment_id,
            checkout_request_id,
        )
        await log_event(
            session_factory,
            "error",
            {"reason": "status_query_failed", "error": type(exc).__name__, "message": str(exc)},
            payment_id,
            checkout_request_id,
        )
        return
    await log_event(
        session_factory,
        "status_query",
        {
            "state": query.state,
            "result_code": query.result_code,
            "description": query.description,
            "response": query.response,
        },
        payment_id,
        checkout_request_id,
    )
    if query.state == STATE_PENDING:
        return
    # A query result has no receipt and no amount, so the amount comparison is skipped and the
    # receipt stays null; a later callback cannot add it because a completed payment is final.
    await apply_payment_result(
        session_factory,
        checkout_request_id,
        query.state,
        result_code=query.result_code,
        result_desc=query.description,
        receipt=None,
        amount=None,
        source=SOURCE_QUERY,
    )


async def maybe_reconcile_payment(
    session_factory: SessionFactory, order: Order, viewer: User, client: DarajaClient | None = None
) -> None:
    """Bring the latest payment of ``order`` up to date before the owning buyer sees it. Never raises.

    1. A stale ``initiating`` payment without a checkout id is failed (``request_not_sent``).
    2. An ``initiated`` payment older than ``RECONCILE_AFTER_SECONDS``, or a ``timeout`` payment younger
       than ``MPESA_TIMEOUT_RECONCILE_WINDOW_MINUTES``, first gets orphan callbacks applied, then (at most
       once per ``MPESA_QUERY_MIN_INTERVAL_SECONDS``) an STK Push Query. No transaction is open during it.
    Concurrent polls are safe because ``apply_payment_result`` is idempotent. Admin views never reconcile.
    """
    if not _viewer_owns(viewer, order):
        return
    try:
        await _reconcile(session_factory, order.id, client)
    except Exception as exc:  # noqa: BLE001 - an order view must never fail because of reconciliation
        logger.warning("Payment reconciliation failed: %s", type(exc).__name__)
        await log_event(session_factory, "error", {"reason": "reconciliation_failed", "error": type(exc).__name__})


# Starting a payment --------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentStart:
    message: str
    stk_target_masked: str

    @property
    def prompt(self) -> str:
        return f"Prompt sent to {self.stk_target_masked}"


def validate_idempotency_key(raw_key: str | None) -> str:
    if raw_key is None or not raw_key.strip():
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "idempotency_key_required",
            "The Idempotency-Key header is required.",
        )
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(raw_key):
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_idempotency_key",
            "Idempotency-Key must be 8 to 64 characters of A-Z, a-z, 0-9, _ or -.",
        )
    return raw_key


def stored_idempotency_key(buyer: User, order_id: UUID, raw_key: str) -> str:
    """Scope the client key to the buyer and order so keys cannot collide or be probed across them."""
    return f"{buyer.id}:{order_id}:{raw_key}"


async def _reserve_payment(
    session: AsyncSession, settings: Settings, buyer: User, order_id: UUID, key: str
) -> tuple[Payment, Order] | PaymentStart:
    """Phase 1, one short transaction: validate the order, then insert an ``initiating`` payment."""
    existing = await session.scalar(select(Payment).where(Payment.idempotency_key == key))
    if existing is not None:
        await session.commit()
        message = PROMPT_MESSAGE if existing.status in OPEN_STATUSES else "This payment request was already handled."
        return PaymentStart(message, mask_phone(existing.stk_phone_number))

    # Lock order: the order row first; payments are read and locked after it, listings never.
    order = await session.scalar(
        select(Order).where(Order.id == order_id, Order.buyer_id == buyer.id).with_for_update()
    )
    if order is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "order_not_found", "Order not found.")
    if order.status != OrderStatus.PENDING_PAYMENT:
        raise AppError(status.HTTP_409_CONFLICT, "order_not_payable", "This order cannot be paid.")

    db_now = await session.scalar(select(func.now()))
    time_left = order.expires_at - db_now
    if time_left < timedelta(seconds=settings.mpesa_min_seconds_before_expiry):
        raise AppError(
            status.HTTP_409_CONFLICT,
            "order_expiring",
            "This order expires too soon to start a payment.",
            reason="expired" if time_left <= timedelta(0) else "too_close",
        )

    open_payments = (
        await session.scalars(
            select(Payment).where(Payment.order_id == order.id, Payment.status.in_(OPEN_STATUSES)).with_for_update()
        )
    ).all()
    stale_after = timedelta(seconds=settings.mpesa_stale_initiating_seconds)
    for payment in open_payments:
        if (
            payment.status == PaymentStatus.INITIATING
            and payment.checkout_request_id is None
            and db_now - payment.created_at >= stale_after
        ):
            payment.status = PaymentStatus.FAILED
            payment.result_desc = REQUEST_NOT_SENT
        else:
            raise AppError(status.HTTP_409_CONFLICT, "payment_in_progress", "A payment for this order is in progress.")
    if open_payments:
        await session.flush()

    attempts = await session.scalar(select(func.count(Payment.id)).where(Payment.order_id == order.id))
    if (attempts or 0) >= settings.mpesa_max_attempts_per_order:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "max_attempts_reached",
            "This order has reached the maximum number of payment attempts.",
        )
    target = stk_target(settings, buyer)
    recent = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.stk_phone_number == target, Payment.created_at > func.now() - timedelta(hours=1)
        )
    )
    if (recent or 0) >= settings.mpesa_phone_attempts_per_hour:
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too_many_payment_attempts",
            "Too many payment requests to this phone. Try again later.",
        )

    payment = Payment(
        id=uuid4(),
        order_id=order.id,
        idempotency_key=key,
        phone_number=buyer.phone_number,
        stk_phone_number=target,
        amount=order.total_amount,
        status=PaymentStatus.INITIATING,
        environment="sandbox",
    )
    session.add(payment)
    try:
        # Committing releases the order lock before any network call.
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        detail = str(exc.orig)
        if "payments_one_open_per_order_idx" in detail or "payments_idempotency_key_key" in detail:
            raise AppError(
                status.HTTP_409_CONFLICT, "payment_in_progress", "A payment for this order is in progress."
            ) from None
        raise
    return payment, order


async def _record_push_outcome(
    session: AsyncSession, order_id: UUID, payment_id: UUID, result: StkPushResult | None, failure: str | None
) -> None:
    """Phase 3, a new short transaction: lock the order, then the payment, and store the push outcome."""
    _, payment = await _lock_order_then_payment(session, order_id, payment_id)
    if payment is not None and result is not None and failure is None:
        if payment.status == PaymentStatus.INITIATING:
            payment.checkout_request_id = result.checkout_request_id
            payment.merchant_request_id = result.merchant_request_id
            payment.status = PaymentStatus.INITIATED
        elif payment.status == PaymentStatus.TIMEOUT and payment.checkout_request_id is None:
            # The expiry job timed the payment out during the call; keep the ids so a late payment can be matched.
            payment.checkout_request_id = result.checkout_request_id
            payment.merchant_request_id = result.merchant_request_id
    elif payment is not None and payment.status == PaymentStatus.INITIATING:
        payment.status = PaymentStatus.FAILED
        payment.result_desc = failure
    await session.commit()


async def initiate_payment(
    session: AsyncSession,
    session_factory: SessionFactory,
    client: DarajaClient | None,
    buyer: User,
    order_id: UUID,
    raw_key: str,
) -> PaymentStart:
    """Start an STK Push for the buyer's order: reserve a payment row, call Daraja, then save the ids.

    The client never supplies an amount, phone number or order data. On a Daraja timeout the prompt may
    still have been sent, so a duplicate prompt is possible in the sandbox when the buyer retries.
    """
    settings = get_settings()
    require_payments_configured(settings, client)
    assert client is not None
    reserved = await _reserve_payment(
        session, settings, buyer, order_id, stored_idempotency_key(buyer, order_id, raw_key)
    )
    if isinstance(reserved, PaymentStart):
        return reserved
    payment, order = reserved
    masked_target = mask_phone(payment.stk_phone_number)

    # Phase 2: no transaction is open from here until the Daraja call returns.
    await log_event(
        session_factory,
        "stk_request",
        {
            "TransactionType": "CustomerPayBillOnline",
            "Amount": payment.amount,
            "PhoneNumber": payment.stk_phone_number,
            "AccountReference": order.reference,
            "TransactionDesc": settings.mpesa_transaction_desc,
        },
        payment.id,
    )
    result: StkPushResult | None = None
    failure: str | None = None
    detail: dict[str, Any] = {}
    try:
        result = await client.stk_push(
            payment.stk_phone_number, payment.amount, order.reference, callback_url(settings)
        )
    except DarajaTimeout as exc:
        failure, detail = REQUEST_UNCONFIRMED, {"error": type(exc).__name__, "message": str(exc)}
    except DarajaError as exc:
        failure, detail = REQUEST_REJECTED, {"error": type(exc).__name__, "message": str(exc)}
    else:
        if not result.accepted:
            failure = REQUEST_REJECTED
            detail = {"description": result.description, "response": result.response}

    await _record_push_outcome(session, order.id, payment.id, result, failure)
    if failure is not None or result is None:
        await log_event(session_factory, "error", {"reason": failure, **detail}, payment.id)
        raise AppError(status.HTTP_502_BAD_GATEWAY, "payment_request_failed", PAYMENT_FAILED_MESSAGE)

    checkout_request_id = result.checkout_request_id
    await log_event(
        session_factory,
        "stk_response",
        {"request": result.request, "response": result.response},
        payment.id,
        checkout_request_id,
    )
    if checkout_request_id is not None:
        try:
            await rematch_orphan_events(session_factory, checkout_request_id)
        except Exception as exc:  # noqa: BLE001 - the prompt was sent; reconciliation retries the match later
            logger.warning("Matching early callbacks failed: %s", type(exc).__name__)
            await log_event(
                session_factory,
                "error",
                {"reason": "orphan_rematch_failed", "error": type(exc).__name__},
                payment.id,
                checkout_request_id,
            )
    return PaymentStart(PROMPT_MESSAGE, masked_target)
