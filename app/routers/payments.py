"""Public Daraja callback endpoint, called by Safaricom only.

The secret token in the path is the only authentication. A wrong token gets an empty 404. After a
valid token the response is always 200 with the acknowledgement, so Daraja never retries, and no
internal error is ever returned. There is deliberately no rate limit here, so genuine callbacks are
never dropped.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import get_settings
from app.db import get_session_factory
from app.services import payment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

ACKNOWLEDGEMENT = {"ResultCode": 0, "ResultDesc": "Accepted"}


def _token_matches(token: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read the body, returning None as soon as it exceeds ``limit`` bytes (declared or actual)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


@router.post("/callback/{token}", include_in_schema=False)
async def payment_callback(
    token: str,
    request: Request,
    session_factory: payment_service.SessionFactory = Depends(get_session_factory),
) -> Response:
    settings = get_settings()
    if not _token_matches(token, settings.mpesa_callback_token):
        return Response(status_code=404)
    body = await _read_capped(request, settings.callback_max_bytes)
    if body is None:
        return Response(status_code=413)
    try:
        await payment_service.process_callback(session_factory, body)
    except Exception as exc:  # noqa: BLE001 - Daraja must always get the acknowledgement
        logger.error("Payment callback processing failed: %s", type(exc).__name__)
        await payment_service.log_event(
            session_factory, "error", {"reason": "callback_processing_failed", "error": type(exc).__name__}
        )
    return JSONResponse(ACKNOWLEDGEMENT)
