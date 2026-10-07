-- Market reference data, observed prices, and forecasts written by the ML pipeline.
create table public.market_locations (
    market_name text primary key,
    fews_market_id text not null unique
);

-- Mombasa's FEWS ID must be re-verified against the maize product.
insert into public.market_locations (market_name, fews_market_id)
values
    ('Nairobi', '58100'),
    ('Kisumu', '57999'),
    ('Mombasa', '58073'),
    ('Nakuru', '254212')
on conflict do nothing;

create table public.market_prices (
    market text not null references public.market_locations(market_name),
    crop public.crop_type not null,
    month date not null,
    avg_price_per_kg numeric(12, 2) not null,
    source public.price_source not null,
    fetched_at timestamptz not null default now(),
    constraint market_prices_month_first_day check (extract(day from month) = 1),
    constraint market_prices_price_positive check (avg_price_per_kg > 0),
    constraint market_prices_pkey primary key (market, crop, month)
);

create index market_prices_month_idx on public.market_prices (month);

create table public.forecasts (
    market text not null references public.market_locations(market_name),
    crop public.crop_type not null,
    horizon public.forecast_horizon not null,
    origin_month date not null,
    target_month date not null,
    origin_price numeric(12, 2) not null,
    predicted_log_change numeric(8, 4) not null,
    predicted_price numeric(12, 2) not null,
    shap_values jsonb not null,
    is_backfill boolean not null default false,
    generated_at timestamptz not null default now(),
    constraint forecasts_origin_first_day check (extract(day from origin_month) = 1),
    constraint forecasts_target_first_day check (extract(day from target_month) = 1),
    constraint forecasts_origin_price_positive check (origin_price > 0),
    constraint forecasts_predicted_price_positive check (predicted_price > 0),
    constraint forecasts_target_matches_horizon check (
        (horizon = 'one_month' and target_month = (origin_month + interval '1 month')::date)
        or (horizon = 'two_month' and target_month = (origin_month + interval '2 months')::date)
    ),
    constraint forecasts_pkey primary key (market, crop, horizon, origin_month)
);

create index forecasts_target_month_idx on public.forecasts (target_month);
create index forecasts_series_latest_idx on public.forecasts (market, crop, horizon, origin_month desc);
