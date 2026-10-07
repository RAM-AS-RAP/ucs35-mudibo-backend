-- Farmer listings and inventory invariants.
create table public.produce_listings (
    id uuid primary key default gen_random_uuid(),
    farmer_id uuid not null,
    farmer_role public.user_role not null default 'farmer',
    crop public.crop_type not null,
    market text not null references public.market_locations(market_name),
    quantity_available numeric(10, 2) not null,
    unit text not null default 'kg',
    price_per_unit numeric(12, 2) not null,
    image_path text null,
    status public.listing_status not null default 'active',
    removed_at timestamptz null,
    removed_by uuid null references public.users(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint produce_listings_farmer_role check (farmer_role = 'farmer'),
    constraint produce_listings_quantity_nonnegative check (quantity_available >= 0),
    constraint produce_listings_price_positive check (price_per_unit > 0),
    constraint produce_listings_farmer_fk foreign key (farmer_id, farmer_role)
        references public.users (id, role) on delete restrict,
    constraint produce_listings_active_has_stock check (status <> 'active' or quantity_available > 0),
    constraint produce_listings_sold_out_empty check (status <> 'sold_out' or quantity_available = 0),
    constraint produce_listings_removed_timestamp check ((status = 'removed') = (removed_at is not null))
);

create index produce_listings_farmer_id_idx on public.produce_listings (farmer_id);
create index produce_listings_market_crop_status_idx on public.produce_listings (market, crop, status);

create or replace function public.prevent_listing_farmer_change()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.farmer_id is distinct from old.farmer_id then
        raise exception 'A listing owner cannot be changed';
    end if;
    return new;
end;
$$;

create trigger produce_listings_owner_immutable
before update on public.produce_listings
for each row execute function public.prevent_listing_farmer_change();

create trigger produce_listings_set_updated_at
before update on public.produce_listings
for each row execute function public.set_updated_at();
