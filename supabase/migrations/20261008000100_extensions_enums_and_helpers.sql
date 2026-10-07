-- Shared PostgreSQL extensions, enum types, and trigger helpers.
create extension if not exists pg_cron with schema pg_catalog;
grant usage on schema cron to postgres;

create type public.user_role as enum ('farmer', 'buyer', 'admin');
create type public.crop_type as enum ('maize', 'beans');
create type public.forecast_horizon as enum ('one_month', 'two_month');
create type public.price_source as enum ('fews', 'wfp');
create type public.listing_status as enum ('active', 'sold_out', 'removed');
create type public.order_status as enum (
    'pending_payment', 'paid', 'payment_failed', 'expired', 'cancelled', 'needs_reconciliation'
);
create type public.payment_status as enum ('initiating', 'initiated', 'completed', 'failed', 'cancelled', 'timeout');

alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create or replace function public.direction_class(p_value numeric)
returns smallint
language sql
immutable
set search_path = public
as $$
    select case
        when p_value > 0.01 then 1::smallint
        when p_value < -0.01 then -1::smallint
        else 0::smallint
    end;
$$;
