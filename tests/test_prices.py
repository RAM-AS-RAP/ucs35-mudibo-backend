"""Crop query normalization and validation for the current prices endpoint."""

from types import SimpleNamespace

import pytest

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import CropType
from app.services import price_service


async def fake_db():
    yield object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_crop", "expected"),
    [
        ("maize", CropType.MAIZE),
        ("MAIZE", CropType.MAIZE),
        ("MaIzE", CropType.MAIZE),
        (" BEANS ", CropType.BEANS),
    ],
)
async def test_current_prices_normalizes_supported_crops(api_client, application, monkeypatch, query_crop, expected):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace()
    application.dependency_overrides[get_db] = fake_db
    received = {}

    async def current_prices(session, crop):
        received["crop"] = crop
        return []

    monkeypatch.setattr(price_service, "current_prices", current_prices)
    response = await api_client.get("/api/v1/prices/current", params={"crop": query_crop})
    assert response.status_code == 200
    assert received["crop"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("crop", ["sorghum", "", "maizebeans"])
async def test_current_prices_rejects_unsupported_crops_before_service_call(api_client, application, monkeypatch, crop):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace()
    application.dependency_overrides[get_db] = fake_db
    called = False

    async def current_prices(session, crop):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(price_service, "current_prices", current_prices)
    response = await api_client.get("/api/v1/prices/current", params={"crop": crop})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not called
