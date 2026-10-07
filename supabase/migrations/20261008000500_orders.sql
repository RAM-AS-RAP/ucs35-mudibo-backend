-- Buyer orders with immutable price and quantity snapshots.
create table public.orders (
    id uuid primary key default gen_random_uuid(),
    reference text not null unique,
    listing_id uuid not null references public.produce_listings(id) on delete restrict,
    buyer_id uuid not null,
    buyer_role public.user_role not null default 'buyer',
    quantity numeric(10, 2) not null,
    unit_price numeric(12, 2) not null,
    total_amount integer not null,
    status public.order_status not null default 'pending_payment',
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint orders_reference_length check (char_length(reference) between 6 and 12),
    constraint orders_buyer_role check (buyer_role = 'buyer'),
    constraint orders_quantity_positive check (quantity > 0),
    constraint orders_unit_price_positive check (unit_price > 0),
    constraint orders_total_positive check (total_amount > 0),
    constraint orders_buyer_fk foreign key (buyer_id, buyer_role)
        references public.users (id, role) on delete restrict
);

create index orders_buyer_created_idx on public.orders (buyer_id, created_at desc);
create index orders_listing_status_idx on public.orders (listing_id, status);
create index orders_pending_expiry_idx on public.orders (expires_at) where status = 'pending_payment';

create or replace function public.guard_order_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.reference is distinct from old.reference
       or new.listing_id is distinct from old.listing_id
       or new.buyer_id is distinct from old.buyer_id
       or new.quantity is distinct from old.quantity
       or new.unit_price is distinct from old.unit_price
       or new.total_amount is distinct from old.total_amount then
        raise exception 'Order identity and price snapshot fields are immutable';
    end if;
    if old.status = 'paid' and new.status is distinct from old.status then
        raise exception 'A paid order cannot change status';
    end if;
    return new;
end;
$$;

create trigger orders_guard_update
before update on public.orders
for each row execute function public.guard_order_update();

create trigger orders_set_updated_at
before update on public.orders
for each row execute function public.set_updated_at();
