"""Administrator dashboard statistics and forecast accuracy reports (read only, set-based queries)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CropType, ForecastHorizon, ListingStatus, OrderStatus, PaymentStatus
from app.schemas.admin import (
    AccuracyItemOut,
    AccuracyOut,
    AccuracySummaryOut,
    AttentionCounts,
    PipelineSeries,
    PipelineStatus,
    StatsOut,
    UserCounts,
)
from app.schemas.common import Pagination

STALE_INITIATED_MINUTES = 10

ACCURACY_NOTE = (
    "Backfilled forecasts were computed after the fact from the prices available at their origin month, "
    "were never shown live, and are reported separately from live forecasts. Sample sizes are small, so "
    "these figures are indicative only."
)

ATTENTION_SQL = f"""
    select
        (select count(*) from public.payments
          where status = 'initiated' and created_at < now() - interval '{STALE_INITIATED_MINUTES} minutes')
            as payments_initiated_over_10_minutes,
        (select max(fetched_at) from public.market_prices) as last_prices_fetch,
        (select max(generated_at) from public.forecasts) as last_forecast_generated
"""

# Every market with price data crossed with every crop, with its latest price month and the
# origin month of its latest one-month forecast.
PIPELINE_SQL = """
    with markets as (
        select distinct market from public.market_prices
    ), crops as (
        select unnest(enum_range(null::public.crop_type)) as crop
    ), latest_prices as (
        select market, crop, max(month) as latest_price_month
        from public.market_prices
        group by market, crop
    )
    select
        m.market,
        c.crop::text as crop,
        lp.latest_price_month,
        lf.origin_month as forecast_origin_month
    from markets as m
    cross join crops as c
    left join latest_prices as lp on lp.market = m.market and lp.crop = c.crop
    left join public.latest_forecasts as lf
        on lf.market = m.market and lf.crop = c.crop and lf.horizon = 'one_month'
    order by m.market, c.crop
"""


def _counts(rows: dict[tuple[str, str], int], category: str, labels: list[str]) -> dict[str, int]:
    """Counts per label for one dashboard category, with zeros for labels that have no rows."""
    return {label: rows.get((category, label), 0) for label in labels}


async def stats(session: AsyncSession) -> StatsOut:
    """Dashboard counts from ``dashboard_stats``, plus attention and pipeline status: three statements."""
    dashboard = (await session.execute(text("select category, label, value from public.dashboard_stats"))).all()
    values: dict[tuple[str, str], int] = {
        (str(category), str(label)): int(value) for category, label, value in dashboard
    }
    attention = (await session.execute(text(ATTENTION_SQL))).mappings().one()
    series_rows = (await session.execute(text(PIPELINE_SQL))).mappings().all()

    users = _counts(values, "users_by_role", ["farmer", "buyer", "admin"])
    return StatsOut(
        users=UserCounts(
            farmers=users["farmer"],
            buyers=users["buyer"],
            admins=users["admin"],
            disabled=values.get(("users_disabled", "disabled"), 0),
        ),
        listings=_counts(values, "listings_by_status", [item.value for item in ListingStatus]),
        orders=_counts(values, "orders_by_status", [item.value for item in OrderStatus]),
        payments=_counts(values, "payments_by_status", [item.value for item in PaymentStatus]),
        completed_sandbox_amount_kes=values.get(("completed_sandbox_amount", "sandbox"), 0),
        attention=AttentionCounts(
            orders_needing_reconciliation=values.get(("orders_by_status", OrderStatus.NEEDS_RECONCILIATION.value), 0),
            payments_initiated_over_10_minutes=int(attention["payments_initiated_over_10_minutes"] or 0),
        ),
        pipeline=PipelineStatus(
            series=[
                PipelineSeries(
                    market=row["market"],
                    crop=row["crop"],
                    latest_price_month=row["latest_price_month"],
                    forecast_origin_month=row["forecast_origin_month"],
                    forecast_current=(
                        row["latest_price_month"] is not None
                        and row["forecast_origin_month"] == row["latest_price_month"]
                    ),
                )
                for row in series_rows
            ],
            last_prices_fetch=attention["last_prices_fetch"],
            last_forecast_generated=attention["last_forecast_generated"],
        ),
    )


ACCURACY_COLUMNS = """
    market, crop::text as crop, horizon::text as horizon, origin_month, target_month, origin_price,
    predicted_price, actual_price, abs_error, pct_error, actual_log_change, direction_correct,
    persistence_abs_error, beat_persistence, is_backfill, is_pending
"""

ACCURACY_SUMMARY = """
    select
        horizon::text as horizon,
        is_backfill,
        count(*) filter (where not is_pending) as n_realised,
        count(*) filter (where is_pending) as n_pending,
        round(avg(abs(pct_error)) filter (where not is_pending), 2) as mape,
        round(avg(abs_error) filter (where not is_pending), 2) as mean_abs_error,
        round(avg(case when direction_correct then 1 else 0 end) filter (where not is_pending), 2)
            as directional_accuracy,
        round(avg(case when beat_persistence then 1 else 0 end) filter (where not is_pending), 2)
            as beat_persistence_rate
    from public.forecast_accuracy
"""


def _accuracy_filters(
    market: str | None,
    crop: CropType | None,
    horizon: ForecastHorizon | None,
    is_backfill: bool | None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if market is not None:
        clauses.append("lower(market) = lower(:market)")
        params["market"] = market
    if crop is not None:
        clauses.append("crop = cast(:crop as public.crop_type)")
        params["crop"] = crop.value
    if horizon is not None:
        clauses.append("horizon = cast(:horizon as public.forecast_horizon)")
        params["horizon"] = horizon.value
    if is_backfill is not None:
        clauses.append("is_backfill = :is_backfill")
        params["is_backfill"] = is_backfill
    return clauses, params


def _where(clauses: list[str]) -> str:
    return (" where " + " and ".join(clauses)) if clauses else ""


async def forecast_accuracy(
    session: AsyncSession,
    pagination: Pagination,
    *,
    market: str | None = None,
    crop: CropType | None = None,
    horizon: ForecastHorizon | None = None,
    is_backfill: bool | None = None,
    include_pending: bool = True,
) -> AccuracyOut:
    """Rows of ``forecast_accuracy`` (newest origin first) and a summary per horizon and backfill flag.

    The summary uses only realised rows (with an actual price) for its metrics and reports pending rows
    as ``n_pending``; MAPE is the mean of the absolute percentage error.
    """
    clauses, params = _accuracy_filters(market, crop, horizon, is_backfill)
    item_clauses = clauses if include_pending else [*clauses, "not is_pending"]
    # The where clauses are constant fragments from _accuracy_filters; values are always bound parameters.
    total = int(
        await session.scalar(text("select count(*) from public.forecast_accuracy" + _where(item_clauses)), params)
        or 0
    )
    rows = (
        (
            await session.execute(
                text(
                    "select "
                    + ACCURACY_COLUMNS
                    + " from public.forecast_accuracy"
                    + _where(item_clauses)
                    + " order by origin_month desc, target_month desc, market, crop, horizon"
                    + " limit :limit offset :offset"
                ),
                {**params, "limit": pagination.limit, "offset": pagination.offset},
            )
        )
        .mappings()
        .all()
    )
    summary_rows = (
        (
            await session.execute(
                text(
                    ACCURACY_SUMMARY + _where(clauses) + " group by horizon, is_backfill order by horizon, is_backfill"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return AccuracyOut(
        items=[AccuracyItemOut(**row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        summary=[
            AccuracySummaryOut(
                horizon=row["horizon"],
                is_backfill=row["is_backfill"],
                n_realised=int(row["n_realised"]),
                n_pending=int(row["n_pending"]),
                mape=row["mape"],
                mean_abs_error=row["mean_abs_error"],
                directional_accuracy=row["directional_accuracy"],
                beat_persistence_rate=row["beat_persistence_rate"],
            )
            for row in summary_rows
        ],
        note=ACCURACY_NOTE,
    )
