"""Crop query normalization and validation for the current prices endpoint."""

from types import SimpleNamespace

import pytest

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import CropType
from app.schemas.market import CurrentPricesResponse, PriceTrend
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
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    received = {}

    async def current_prices(session, crop):
        received["crop"] = crop
        return CurrentPricesResponse(items=[], as_of_note="test")

    monkeypatch.setattr(price_service, "current_prices", current_prices)
    response = await api_client.get("/api/v1/prices/current", params={"crop": query_crop})
    assert response.status_code == 200
    assert received["crop"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("crop", ["sorghum", "", "maizebeans"])
async def test_current_prices_rejects_unsupported_crops_before_service_call(api_client, application, monkeypatch, crop):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    called = False

    async def current_prices(session, crop):
        nonlocal called
        called = True
        return CurrentPricesResponse(items=[], as_of_note="test")

    monkeypatch.setattr(price_service, "current_prices", current_prices)
    response = await api_client.get("/api/v1/prices/current", params={"crop": crop})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "crop", "months", "expected_market", "expected_crop", "expected_months"),
    [
        ("nairobi", "MAIZE", None, "Nairobi", CropType.MAIZE, 12),
        ("NAKURU", "beans", "6", "Nakuru", CropType.BEANS, 6),
        ("mOmBaSa", "MaIzE", "6", "Mombasa", CropType.MAIZE, 6),
        (" Kisumu ", "BEANS", "12", "Kisumu", CropType.BEANS, 12),
    ],
)
async def test_price_trends_normalizes_supported_query_values(
    api_client, application, monkeypatch, market, crop, months, expected_market, expected_crop, expected_months
):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    received = {}

    async def price_trends(session, market, crop, months):
        received.update(market=market, crop=crop, months=months)
        return PriceTrend(market=market, crop=crop, points=[], first_month=None, last_month=None, count=0)

    monkeypatch.setattr(price_service, "price_trends", price_trends)
    params = {"market": market, "crop": crop}
    if months is not None:
        params["months"] = months
    response = await api_client.get("/api/v1/prices/trends", params=params)
    assert response.status_code == 200
    assert received == {
        "market": expected_market,
        "crop": expected_crop.value,
        "months": expected_months,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"market": "Eldoret", "crop": "maize"},
        {"market": "Nairobi", "crop": "sorghum"},
        {"market": "Nairobi", "crop": "maize", "months": "0"},
        {"market": "Nairobi", "crop": "maize", "months": "61"},
        {"market": "Nairobi", "crop": "maize", "months": "1"},
        {"market": "Nairobi", "crop": "maize", "months": "24"},
    ],
)
async def test_price_trends_rejects_unsupported_values_before_service_call(
    api_client, application, monkeypatch, params
):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    called = False

    async def price_trends(session, market, crop, months):
        nonlocal called
        called = True
        return PriceTrend(market=market, crop=crop, points=[], first_month=None, last_month=None, count=0)

    monkeypatch.setattr(price_service, "price_trends", price_trends)
    response = await api_client.get("/api/v1/prices/trends", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not called
