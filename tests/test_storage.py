"""Mocked Supabase Storage HTTP behavior for listing images."""

import httpx
import pytest

from app.integrations import storage
from app.integrations.storage import StorageError


class FakeResponse:
    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, *, fail_first_post: bool = False, fail_delete: bool = False):
        self.fail_first_post = fail_first_post
        self.fail_delete = fail_delete
        self.post_calls = []
        self.delete_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, *, content, headers):
        self.post_calls.append((url, content, headers))
        if self.fail_first_post and len(self.post_calls) == 1:
            raise httpx.ConnectError("temporary network failure")
        return FakeResponse()

    async def delete(self, url, *, headers):
        self.delete_calls.append((url, headers))
        if self.fail_delete:
            raise httpx.ConnectError("temporary network failure")
        return FakeResponse()


def configure_settings(monkeypatch):
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "supabase_url": "https://storage.example",
                "supabase_storage_bucket": "listing-images",
                "supabase_service_role_key": "service-test-key",
            },
        )(),
    )


@pytest.mark.asyncio
async def test_upload_uses_service_role_headers_and_retries_one_network_error(monkeypatch):
    configure_settings(monkeypatch)
    client = FakeClient(fail_first_post=True)
    monkeypatch.setattr(storage.httpx, "AsyncClient", lambda **kwargs: client)

    await storage.upload_image("listings/id/test.jpg", b"jpeg")

    assert len(client.post_calls) == 2
    _, content, headers = client.post_calls[-1]
    assert content == b"jpeg"
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["x-upsert"] == "false"
    assert headers["apikey"] == "service-test-key"


@pytest.mark.asyncio
async def test_upload_raises_storage_error_after_retry_exhaustion(monkeypatch):
    configure_settings(monkeypatch)
    client = FakeClient(fail_first_post=True)

    async def always_fail(url, *, content, headers):
        client.post_calls.append((url, content, headers))
        raise httpx.ConnectError("network failure")

    client.post = always_fail
    monkeypatch.setattr(storage.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(StorageError):
        await storage.upload_image("listings/id/test.jpg", b"jpeg")
    assert len(client.post_calls) == 2


@pytest.mark.asyncio
async def test_upload_http_error_is_typed_and_not_retried(monkeypatch):
    configure_settings(monkeypatch)
    client = FakeClient()

    async def rejected(url, *, content, headers):
        client.post_calls.append((url, content, headers))
        request = httpx.Request("POST", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    client.post = rejected
    monkeypatch.setattr(storage.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(StorageError):
        await storage.upload_image("listings/id/test.jpg", b"jpeg")
    assert len(client.post_calls) == 1


@pytest.mark.asyncio
async def test_delete_image_returns_false_and_does_not_raise_on_failure(monkeypatch):
    configure_settings(monkeypatch)
    client = FakeClient(fail_delete=True)
    monkeypatch.setattr(storage.httpx, "AsyncClient", lambda **kwargs: client)
    assert await storage.delete_image("listings/id/test.jpg") is False
