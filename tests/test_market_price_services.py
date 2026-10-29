"""Mocked service tests for available markets, monthly prices, and forecasts."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfoNotFoundError

import pytest

from app.core.errors import AppError
from app.models.enums import CropType, ForecastHorizon
from app.schemas.market import CurrentPriceItem
from app.services import forecast_service, market_service, price_service


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, results=(), scalars=()):
        self.results = list(results)
        self.scalars = list(scalars)
        self.statements = []
        self.parameters = []

    async def execute(self, statement, parameters=None):
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        return self.results.pop(0)

    async def scalar(self, statement, parameters=None):
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        return self.scalars.pop(0)


def test_displayed_price_decimals_serialize_as_rounded_json_numbers():
    item = CurrentPriceItem(
        market="Nairobi",
        crop="maize",
        month=date(2026, 8, 1),
        avg_price_per_kg=Decimal("1.235"),
        source="fews",
        fetched_at=datetime(2026, 8, 2, tzinfo=UTC),
        previous_month=None,
        previous_price=None,
        change_pct=None,
    )
    payload = json.loads(item.model_dump_json())
    assert item.avg_price_per_kg == Decimal("1.235")
    assert payload["avg_price_per_kg"] == 1.24
    assert isinstance(payload["avg_price_per_kg"], float)


@pytest.mark.asyncio
async def test_available_markets_come_only_from_price_data():
    session = FakeSession([FakeResult([("Kisumu",), ("Nairobi",)])])
    assert await market_service.get_available_markets(session) == ["Kisumu", "Nairobi"]


@pytest.mark.asyncio
async def test_market_guard_normalizes_case_but_requires_market_data():
    session = FakeSession([FakeResult([("Kisumu",), ("Nairobi",)])])
    assert await market_service.require_available_market(session, " nairobi ") == "Nairobi"

    unavailable = FakeSession([FakeResult([("Kisumu",), ("Nairobi",)])])
    with pytest.raises(AppError) as exc_info:
        await market_service.require_available_market(unavailable, "Mombasa")
    assert exc_info.value.code == "market_not_available"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_current_prices_uses_only_immediately_previous_calendar_month():
    session = FakeSession(
        [
            FakeResult(
                [
                    {
                        "market": "Nairobi",
                        "crop": "maize",
                        "month": date(2026, 8, 1),
                        "avg_price_per_kg": Decimal("110.00"),
                        "source": "fews",
                        "fetched_at": datetime(2026, 8, 2, tzinfo=UTC),
                        "previous_month": date(2026, 7, 1),
                        "previous_price": Decimal("100.00"),
                    },
                    {
                        "market": "Kisumu",
                        "crop": "maize",
                        "month": date(2026, 8, 1),
                        "avg_price_per_kg": Decimal("92.25"),
                        "source": "wfp",
                        "fetched_at": datetime(2026, 8, 2, tzinfo=UTC),
                        "previous_month": None,
                        "previous_price": None,
                    },
                ]
            )
        ]
    )
    response = await price_service.current_prices(session, "maize")
    assert [item.market for item in response.items] == ["Nairobi", "Kisumu"]
    assert response.items[0].change_pct == Decimal("10.00")
    assert response.items[0].previous_month == date(2026, 7, 1)
    assert response.items[1].previous_price is None
    assert response.items[1].change_pct is None
    assert "FEWS NET" in response.as_of_note


@pytest.mark.asyncio
async def test_current_prices_returns_empty_items_when_crop_has_no_rows():
    response = await price_service.current_prices(FakeSession([FakeResult([])]), "beans")
    assert response.items == []
    assert response.as_of_note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("months", "expected_first_month"),
    [(6, date(2026, 3, 1)), (12, date(2025, 9, 1))],
)
async def test_price_trends_counts_back_from_latest_published_month_and_preserves_gaps(months, expected_first_month):
    session = FakeSession(
        [
            FakeResult([("Nairobi",), ("Kisumu",)]),
            FakeResult(
                [
                    {"month": date(2026, 6, 1), "avg_price_per_kg": Decimal("101.25"), "source": "fews"},
                    {"month": date(2026, 8, 1), "avg_price_per_kg": Decimal("104.00"), "source": "fews"},
                ]
            ),
        ],
        scalars=[date(2026, 8, 1)],
    )
    response = await price_service.price_trends(session, " nairobi ", "maize", months)
    assert response.market == "Nairobi"
    assert [point.month for point in response.points] == [date(2026, 6, 1), date(2026, 8, 1)]
    assert response.first_month == date(2026, 6, 1)
    assert response.last_month == date(2026, 8, 1)
    assert response.count == 2
    assert session.parameters[1] == {"market": "Nairobi", "crop": "maize"}
    assert session.parameters[2]["first_month"] == expected_first_month
    assert session.parameters[2]["latest_month"] == date(2026, 8, 1)


@pytest.mark.asyncio
async def test_price_trends_returns_empty_series_when_crop_has_no_prices():
    session = FakeSession([FakeResult([("Nairobi",), ("Kisumu",)])], scalars=[None])
    response = await price_service.price_trends(session, "Kisumu", "beans", 6)
    assert response.points == []
    assert response.count == 0
    assert response.first_month is None
    assert len(session.statements) == 2


def test_current_month_uses_africa_nairobi_timezone() -> None:
    utc_now = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)
    try:
        current_month = market_service.current_month_nairobi(utc_now)
    except ZoneInfoNotFoundError:
        pytest.skip("tzdata is required for Africa/Nairobi timezone tests")
    assert current_month == date(2026, 10, 1)


def forecast_row(origin_month: date) -> dict[str, object]:
    return {
        "market": "Nairobi",
        "crop": "maize",
        "horizon": "one_month",
        "origin_month": origin_month,
        "target_month": date(2026, 10, 1),
        "origin_price": Decimal("100.00"),
        "predicted_price": Decimal("110.52"),
        "predicted_log_change": Decimal("0.1"),
        "shap_values": {"temperature": 0.4},
        "generated_at": datetime(2026, 9, 1, tzinfo=UTC),
        "is_backfill": False,
    }


@pytest.mark.asyncio
async def test_forecast_is_current_when_origin_matches_recent_price_and_shap_is_unchanged(monkeypatch):
    monkeypatch.setattr(forecast_service, "current_month_nairobi", lambda: date(2026, 9, 1))
    shap_values = {"temperature": 0.4}
    row = forecast_row(date(2026, 8, 1))
    row["shap_values"] = shap_values
    session = FakeSession(
        [FakeResult([("Nairobi",), ("Kisumu",)]), FakeResult([row])],
        [date(2026, 8, 1)],
    )
    response = await forecast_service.latest_forecast(session, " NAIROBI ", CropType.MAIZE, ForecastHorizon.ONE_MONTH)
    assert response.market == "Nairobi"
    assert response.expected_change_pct == Decimal("10.52")
    assert response.shap_values is shap_values
    assert response.based_on == date(2026, 8, 1)
    assert response.for_month == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_missing_forecast_returns_no_forecast_reason():
    session = FakeSession([FakeResult([("Nairobi",)]), FakeResult([])])
    with pytest.raises(AppError) as exc_info:
        await forecast_service.latest_forecast(session, "Nairobi", "maize", "one_month")
    assert exc_info.value.reason == "no_forecast"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_month", [date(2026, 7, 1), date(2026, 8, 1)])
async def test_forecast_hides_nonlatest_or_too_old_origin(monkeypatch, origin_month):
    monkeypatch.setattr(forecast_service, "current_month_nairobi", lambda: date(2026, 10, 1))
    latest_price_month = date(2026, 9, 1)
    session = FakeSession(
        [FakeResult([("Nairobi",)]), FakeResult([forecast_row(origin_month)])],
        [latest_price_month],
    )
    with pytest.raises(AppError) as exc_info:
        await forecast_service.latest_forecast(session, "Nairobi", "maize", "one_month")
    assert exc_info.value.reason == "stale"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_forecast_hides_backfill_even_when_origin_matches_latest_price(monkeypatch):
    monkeypatch.setattr(forecast_service, "current_month_nairobi", lambda: date(2026, 10, 1))
    row = forecast_row(date(2026, 10, 1))
    row["is_backfill"] = True
    session = FakeSession(
        [FakeResult([("Nairobi",)]), FakeResult([row])],
        [date(2026, 10, 1)],
    )
    with pytest.raises(AppError) as exc_info:
        await forecast_service.latest_forecast(session, "Nairobi", "maize", "one_month")
    assert exc_info.value.reason == "stale"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_forecast_hides_price_series_older_than_two_months(monkeypatch):
    monkeypatch.setattr(forecast_service, "current_month_nairobi", lambda: date(2026, 10, 1))
    session = FakeSession(
        [FakeResult([("Nairobi",)]), FakeResult([forecast_row(date(2026, 7, 1))])],
        [date(2026, 7, 1)],
    )
    with pytest.raises(AppError) as exc_info:
        await forecast_service.latest_forecast(session, "Nairobi", "maize", "one_month")
    assert exc_info.value.reason == "stale"
