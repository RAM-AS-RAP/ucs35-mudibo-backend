-- Read-only forecast and dashboard views. All execute with caller privileges.
create view public.latest_forecasts
with (security_invoker = on)
as
select distinct on (f.market, f.crop, f.horizon)
    f.market,
    f.crop,
    f.horizon,
    f.origin_month,
    f.target_month,
    f.origin_price,
    f.predicted_log_change,
    f.predicted_price,
    f.shap_values,
    f.is_backfill,
    f.generated_at
from public.forecasts as f
order by f.market, f.crop, f.horizon, f.origin_month desc;

create view public.forecast_accuracy
with (security_invoker = on)
as
with joined as (
    select
        f.market,
        f.crop,
        f.horizon,
        f.origin_month,
        f.target_month,
        f.origin_price,
        f.predicted_log_change,
        f.predicted_price,
        f.shap_values,
        f.is_backfill,
        f.generated_at,
        p.avg_price_per_kg as actual_price
    from public.forecasts as f
    left join public.market_prices as p
        on p.market = f.market
       and p.crop = f.crop
       and p.month = f.target_month
), computed as (
    select
        j.*,
        case when j.actual_price is null then null else abs(j.predicted_price - j.actual_price) end as abs_error,
        case
            when j.actual_price is null then null
            else abs(j.predicted_price - j.actual_price) / j.actual_price * 100
        end as pct_error,
        case
            when j.actual_price is null then null
            else ln(j.actual_price / j.origin_price)
        end as actual_log_change,
        case
            when j.actual_price is null then null
            else abs(j.origin_price - j.actual_price)
        end as persistence_abs_error
    from joined as j
)
select
    c.market,
    c.crop,
    c.horizon,
    c.origin_month,
    c.target_month,
    c.origin_price,
    c.predicted_log_change,
    c.predicted_price,
    c.shap_values,
    c.is_backfill,
    c.generated_at,
    c.actual_price,
    c.abs_error,
    c.pct_error,
    c.actual_log_change,
    case
        when c.actual_price is null then null
        else public.direction_class(c.predicted_log_change) = public.direction_class(c.actual_log_change)
    end as direction_correct,
    c.persistence_abs_error,
    case
        when c.actual_price is null then null
        else c.abs_error < c.persistence_abs_error
    end as beat_persistence,
    c.actual_price is null as is_pending
from computed as c;

create view public.dashboard_stats
with (security_invoker = on)
as
select 'users_by_role'::text as category, u.role::text as label, count(*)::numeric as value
from public.users as u
group by u.role
union all
select 'users_disabled'::text, 'disabled'::text, count(*)::numeric
from public.users as u
where u.is_disabled
union all
select 'listings_by_status'::text, l.status::text, count(*)::numeric
from public.produce_listings as l
group by l.status
union all
select 'orders_by_status'::text, o.status::text, count(*)::numeric
from public.orders as o
group by o.status
union all
select 'payments_by_status'::text, p.status::text, count(*)::numeric
from public.payments as p
group by p.status
union all
select
    'completed_sandbox_amount'::text,
    'sandbox'::text,
    coalesce(sum(p.amount), 0)::numeric
from public.payments as p
where p.status = 'completed'
  and p.environment = 'sandbox';
