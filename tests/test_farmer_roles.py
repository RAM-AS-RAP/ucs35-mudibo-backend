"""Farmer-module authorization matrix using dependency overrides."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_current_user
from app.db import get_db
from app.integrations.storage import StorageError
from app.models.enums import UserRole
from app.schemas.listing import ListingOut
from app.schemas.market import CurrentPricesResponse, MarketSummary, PriceTrend
from app.services import forecast_service, listing_service, price_service


async def fake_db():
    yield object()


def install_user(application, role=None):
    user = None
    if role is not None:
        user = SimpleNamespace(
            id=uuid4(), role=role, is_disabled=False, must_change_password=False, is_super_admin=False
        )
        application.dependency_overrides[get_current_user] = lambda: user
    application.dependency_overrides[get_db] = fake_db
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/markets",
        "/api/v1/prices/current?crop=maize",
        "/api/v1/prices/trends?market=Nairobi&crop=maize",
        "/api/v1/forecasts?market=Nairobi&crop=maize&horizon=one_month",
        "/api/v1/listings/mine",
        "/api/v1/listings/mine/orders",
    ],
)
async def test_buyer_is_forbidden_from_farmer_module(api_client, application, path):
    install_user(application, UserRole.BUYER)
    response = await api_client.get(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("get", "/api/v1/listings/mine"), ("get", "/api/v1/listings/mine/orders")])
async def test_admin_is_forbidden_from_farmer_listing_routes(api_client, application, method, path):
    install_user(application, UserRole.ADMIN)
    response = await getattr(api_client, method)(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_buyer_is_forbidden_from_create_update_and_delete_listing_routes(api_client, application):
    install_user(application, UserRole.BUYER)
    listing_id = uuid4()
    create = await api_client.post(
        "/api/v1/listings",
        data={
            "crop": "maize",
            "market": "Nairobi",
            "quantity_available": "1",
            "unit": "kg",
            "price_per_unit": "10",
        },
    )
    update = await api_client.patch(f"/api/v1/listings/{listing_id}", data={"quantity_available": "2"})
    delete = await api_client.delete(f"/api/v1/listings/{listing_id}")
    assert [create.status_code, update.status_code, delete.status_code] == [403, 403, 403]
    assert all(response.json()["error"]["code"] == "forbidden" for response in (create, update, delete))


@pytest.mark.asyncio
async def test_farmer_can_create_a_listing(api_client, application, monkeypatch):
    farmer = install_user(application, UserRole.FARMER)

    async def create_listing(session, user, values, image):
        assert user.role == UserRole.FARMER
        assert values["unit"] == "kg"
        return ListingOut(
            id=uuid4(),
            farmer_id=farmer.id,
            crop="maize",
            market="Nairobi",
            quantity_available="2.00",
            unit="kg",
            price_per_unit="10.00",
            image_url=None,
            status="active",
            created_at=date(2026, 10, 10),
            updated_at=date(2026, 10, 10),
        )

    monkeypatch.setattr(listing_service, "create_listing", create_listing)
    response = await api_client.post(
        "/api/v1/listings",
        files={
            "crop": (None, "maize"),
            "market": (None, "Nairobi"),
            "quantity_available": (None, "2"),
            "price_per_unit": (None, "10"),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["farmer_id"] == str(farmer.id)


@pytest.mark.asyncio
async def test_listing_create_rejects_unknown_multipart_fields(api_client, application):
    install_user(application, UserRole.FARMER)
    response = await api_client.post(
        "/api/v1/listings",
        files={
            "crop": (None, "maize"),
            "market": (None, "Nairobi"),
            "quantity_available": (None, "2"),
            "price_per_unit": (None, "10"),
            "farmer_id": (None, str(uuid4())),
        },
    )
    assert response.status_code == 422
    assert "farmer_id" in response.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_listing_upload_failure_returns_safe_server_error(application, monkeypatch):
    install_user(application, UserRole.FARMER)

    async def failed_create(session, user, values, image):
        raise StorageError("private storage error")

    monkeypatch.setattr(listing_service, "create_listing", failed_create)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/listings",
            files={
                "crop": (None, "maize"),
                "market": (None, "Nairobi"),
                "quantity_available": (None, "2"),
                "unit": (None, "kg"),
                "price_per_unit": (None, "10"),
            },
        )
    assert response.status_code == 500
    assert "private storage error" not in response.text


@pytest.mark.asyncio
async def test_listing_create_rate_limit_is_30_per_hour(api_client, application, monkeypatch):
    farmer = install_user(application, UserRole.FARMER)

    async def fake_create(session, user, values, image):
        return ListingOut(
            id=uuid4(),
            farmer_id=farmer.id,
            crop="maize",
            market="Nairobi",
            quantity_available="2.00",
            unit="kg",
            price_per_unit="10.00",
            image_url=None,
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(listing_service, "create_listing", fake_create)
    body = {
        "crop": (None, "maize"),
        "market": (None, "Nairobi"),
        "quantity_available": (None, "2"),
        "unit": (None, "kg"),
        "price_per_unit": (None, "10"),
    }
    responses = [await api_client.post("/api/v1/listings", files=body) for _ in range(31)]
    assert all(response.status_code == 201 for response in responses[:30])
    assert responses[30].status_code == 429
    assert responses[30].json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_admin_can_read_markets_prices_and_forecasts(api_client, application, monkeypatch):
    install_user(application, UserRole.ADMIN)

    async def markets(session):
        return [MarketSummary(name="Nairobi", latest_month=date(2026, 9, 1))]

    async def current_prices(session, crop):
        return CurrentPricesResponse(items=[], as_of_note="test")

    async def price_trends(session, market, crop, months):
        return PriceTrend(market=market, crop=crop, points=[], first_month=None, last_month=None, count=0)

    async def latest_forecast(session, market, crop, horizon):
        return {"market": market, "crop": crop, "horizon": horizon}

    monkeypatch.setattr(price_service, "markets", markets)
    monkeypatch.setattr(price_service, "current_prices", current_prices)
    monkeypatch.setattr(price_service, "price_trends", price_trends)
    monkeypatch.setattr(forecast_service, "latest_forecast", latest_forecast)
    responses = [
        await api_client.get("/api/v1/markets"),
        await api_client.get("/api/v1/prices/current?crop=maize"),
        await api_client.get("/api/v1/prices/trends?market=Nairobi&crop=maize"),
        await api_client.get("/api/v1/forecasts?market=Nairobi&crop=maize&horizon=one_month"),
    ]
    assert [response.status_code for response in responses] == [200, 200, 200, 200]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        (
            "post",
            "/api/v1/listings",
            {"crop": "maize", "market": "Nairobi", "quantity_available": "1", "unit": "kg", "price_per_unit": "10"},
        ),
        ("get", "/api/v1/listings/mine", None),
        ("get", "/api/v1/listings/mine/orders", None),
        ("patch", f"/api/v1/listings/{uuid4()}", {"quantity_available": "1"}),
        ("delete", f"/api/v1/listings/{uuid4()}", None),
    ],
)
async def test_buyer_is_forbidden_from_every_farmer_listing_route(api_client, application, method, path, data):
    install_user(application, UserRole.BUYER)
    response = (
        await getattr(api_client, method)(path, data=data)
        if data is not None
        else await getattr(api_client, method)(path)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_missing_token_is_401_for_farmer_module(api_client, application):
    application.dependency_overrides[get_db] = fake_db
    response = await api_client.get("/api/v1/markets")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_module_three_does_not_register_buyer_browse_or_detail_routes(application):
    paths = application.openapi()["paths"]
    assert paths["/api/v1/listings"].keys() == {"post"}
    assert "/api/v1/listings/{listing_id}" in paths
    assert "get" not in paths["/api/v1/listings/{listing_id}"]
    assert paths["/api/v1/listings/{listing_id}"].keys() == {"patch", "delete"}


def test_listing_create_openapi_exposes_multipart_fields(application):
    schema = application.openapi()
    operation = schema["paths"]["/api/v1/listings"]["post"]
    request_body = operation["requestBody"]
    assert "multipart/form-data" in request_body["content"]
    schema_ref = request_body["content"]["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
    properties = schema["components"]["schemas"][schema_ref]["properties"]
    assert set(properties) == {"crop", "market", "quantity_available", "unit", "price_per_unit", "image"}
