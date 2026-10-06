"""Supabase Storage operations over its HTTP API."""

from urllib.parse import quote

import httpx

from app.core.config import get_settings


def _object_url(path: str) -> str:
    settings = get_settings()
    safe_path = quote(path, safe="/")
    return f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{settings.supabase_storage_bucket}/{safe_path}"


async def upload_image(path: str, content: bytes, content_type: str) -> None:
    """Upload image bytes without exposing the service-role credential."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _object_url(path),
            content=content,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": content_type,
                "x-upsert": "false",
            },
        )
        response.raise_for_status()


async def delete_image(path: str) -> None:
    """Delete an uploaded object after a failed listing insert."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            _object_url(path),
            headers={"Authorization": f"Bearer {settings.supabase_service_role_key}"},
        )
        response.raise_for_status()


def public_url(path: str) -> str:
    """Return the public URL for an object in the configured listing bucket."""
    settings = get_settings()
    base_url = settings.supabase_url.rstrip("/")
    safe_path = quote(path, safe="/")
    return f"{base_url}/storage/v1/object/public/{settings.supabase_storage_bucket}/{safe_path}"
