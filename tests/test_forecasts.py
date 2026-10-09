"""Forecast query normalization and validation without database access."""

from types import SimpleNamespace

import pytest

from app.core.dependencies import get_current_user
from app.db import get_db
from app.models.enums import CropType, ForecastHorizon
from app.services import forecast_service


async def fake_db():
    yield object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "crop", "horizon", "expected_market", "expected_crop", "expected_horizon"),
    [
        (" nairobi ", "MAIZE", "1", "Nairobi", CropType.MAIZE, ForecastHorizon.ONE_MONTH),
        ("NAKURU", " beans ", "2", "Nakuru", CropType.BEANS, ForecastHorizon.TWO_MONTH),
        ("mOmBaSa", "MaIzE", "one_month", "Mombasa", CropType.MAIZE, ForecastHorizon.ONE_MONTH),
        ("KISUMU", "BEANS", " TWO_MONTH ", "Kisumu", CropType.BEANS, ForecastHorizon.TWO_MONTH),
    ],
)
async def test_forecast_normalizes_supported_query_values(
    api_client,
    application,
    monkeypatch,
    market,
    crop,
    horizon,
    expected_market,
    expected_crop,
    expected_horizon,
):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    received = {}

    async def latest_forecast(session, market, crop, horizon):
        received.update(market=market, crop=crop, horizon=horizon)
        return {"ok": True}

    monkeypatch.setattr(forecast_service, "latest_forecast", latest_forecast)
    response = await api_client.get("/api/v1/forecasts", params={"market": market, "crop": crop, "horizon": horizon})
    assert response.status_code == 200
    assert received == {
        "market": expected_market,
        "crop": expected_crop,
        "horizon": expected_horizon,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"market": "Eldoret", "crop": "maize", "horizon": "1"},
        {"market": "Nairobi", "crop": "sorghum", "horizon": "1"},
        {"market": "Nairobi", "crop": "maize", "horizon": "3"},
    ],
)
async def test_forecast_rejects_unsupported_values_before_service_call(api_client, application, monkeypatch, params):
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="farmer")
    application.dependency_overrides[get_db] = fake_db
    called = False

    async def latest_forecast(session, market, crop, horizon):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(forecast_service, "latest_forecast", latest_forecast)
    response = await api_client.get("/api/v1/forecasts", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not called
