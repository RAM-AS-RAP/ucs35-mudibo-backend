"""Supabase Storage operations over its HTTP API."""

import logging
from urllib.parse import quote

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Raised when a Storage upload cannot be completed."""


def _object_url(path: str) -> str:
    settings = get_settings()
    safe_path = quote(path, safe="/")
    return f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{settings.supabase_storage_bucket}/{safe_path}"


async def upload_image(path: str, content: bytes) -> None:
    """Upload image bytes without exposing the service-role credential."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
        "Content-Type": "image/jpeg",
        "x-upsert": "false",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        for attempt in range(2):
            try:
                response = await client.post(_object_url(path), content=content, headers=headers)
                response.raise_for_status()
                return
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 1:
                    raise StorageError("Storage upload failed due to a network error") from exc
            except httpx.HTTPStatusError as exc:
                raise StorageError("Storage upload was rejected") from exc


async def delete_image(path: str) -> bool:
    """Delete an object best-effort; cleanup errors never escape to the caller."""
    try:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.delete(
                _object_url(path),
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "apikey": settings.supabase_service_role_key,
                },
            )
            response.raise_for_status()
    except Exception:
        logger.warning("Storage object cleanup failed for %s", path)
        return False
    return True


def public_url(path: str) -> str:
    """Return the public URL for an object in the configured listing bucket."""
    settings = get_settings()
    base_url = settings.supabase_url.rstrip("/")
    safe_path = quote(path, safe="/")
    return f"{base_url}/storage/v1/object/public/{settings.supabase_storage_bucket}/{safe_path}"
