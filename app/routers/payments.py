"""Public Daraja callback endpoint."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import constant_time_equal
from app.db import get_db
from app.services.payment_service import handle_callback

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/callback/{token}")
async def payment_callback(
    token: str, payload: dict[str, Any], session: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    expected = get_settings().mpesa_callback_token
    if not expected or not constant_time_equal(token, expected):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    await handle_callback(session, payload)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
