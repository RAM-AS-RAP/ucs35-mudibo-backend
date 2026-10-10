-- Least-privilege credentials and row policies for the monthly ML pipeline.
-- The password is deliberately not set here. Set it once by hand in the Supabase SQL Editor with
-- alter role pipeline_writer with password '<value>'; store that value only in the ML repository's GitHub secret.
-- A role without a password cannot authenticate with password-based login.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'pipeline_writer') then
        create role pipeline_writer
            login
            nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls
            connection limit 5;
    end if;
end
$$;

-- This membership lets the verification script use SET ROLE. Remove it if the hosted project rejects the grant;
-- use the manual connection test documented in README.md instead.
grant pipeline_writer to postgres;

alter role pipeline_writer set search_path = public;
alter role pipeline_writer set statement_timeout = '30s';
alter role pipeline_writer set idle_in_transaction_session_timeout = '30s';
alter role pipeline_writer set lock_timeout = '10s';

-- Start from no object access and grant only the pipeline's required operations.
revoke all on all tables in schema public from pipeline_writer;
revoke all on all sequences in schema public from pipeline_writer;
revoke all on all functions in schema public from pipeline_writer;
revoke create on schema public from pipeline_writer;
grant usage on schema public to pipeline_writer;

grant select on public.market_locations to pipeline_writer;

grant select, insert on public.market_prices to pipeline_writer;
grant update (avg_price_per_kg, source, fetched_at) on public.market_prices to pipeline_writer;

grant select, insert on public.forecasts to pipeline_writer;
grant update (
    target_month,
    origin_price,
    predicted_log_change,
    predicted_price,
    shap_values,
    is_backfill,
    generated_at
) on public.forecasts to pipeline_writer;

-- New tables remain unavailable to pipeline_writer until explicitly granted here.
-- The upsert path requires the select, insert, and update policies together.
drop policy if exists pipeline_select_market_locations on public.market_locations;
create policy pipeline_select_market_locations
    on public.market_locations
    for select
    to pipeline_writer
    using (true);

drop policy if exists pipeline_select_market_prices on public.market_prices;
create policy pipeline_select_market_prices
    on public.market_prices
    for select
    to pipeline_writer
    using (true);

drop policy if exists pipeline_insert_market_prices on public.market_prices;
create policy pipeline_insert_market_prices
    on public.market_prices
    for insert
    to pipeline_writer
    with check (true);

drop policy if exists pipeline_update_market_prices on public.market_prices;
create policy pipeline_update_market_prices
    on public.market_prices
    for update
    to pipeline_writer
    using (true)
    with check (true);

drop policy if exists pipeline_select_forecasts on public.forecasts;
create policy pipeline_select_forecasts
    on public.forecasts
    for select
    to pipeline_writer
    using (true);

drop policy if exists pipeline_insert_forecasts on public.forecasts;
create policy pipeline_insert_forecasts
    on public.forecasts
    for insert
    to pipeline_writer
    with check (true);

drop policy if exists pipeline_update_forecasts on public.forecasts;
create policy pipeline_update_forecasts
    on public.forecasts
    for update
    to pipeline_writer
    using (true)
    with check (true);
