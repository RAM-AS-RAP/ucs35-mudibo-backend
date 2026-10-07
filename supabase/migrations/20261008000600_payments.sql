-- Daraja sandbox payment attempts and append-only payment events.
create table public.payments (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders(id) on delete restrict,
    idempotency_key text not null unique,
    phone_number text not null,
    stk_phone_number text not null,
    amount integer not null,
    checkout_request_id text null unique,
    merchant_request_id text null,
    mpesa_receipt_number text null,
    status public.payment_status not null default 'initiating',
    result_code integer null,
    result_desc text null,
    environment text not null default 'sandbox',
    callback_received_at timestamptz null,
    created_at timestamptz not null default now(),
    completed_at timestamptz null,
    updated_at timestamptz not null default now(),
    constraint payments_phone_number_format check (phone_number ~ '^254[17][0-9]{8}$'),
    constraint payments_stk_phone_number_format check (stk_phone_number ~ '^254[17][0-9]{8}$'),
    constraint payments_amount_positive check (amount > 0),
    constraint payments_environment_allowed check (environment in ('sandbox', 'production'))
);

create unique index payments_receipt_unique_idx
    on public.payments (mpesa_receipt_number) where mpesa_receipt_number is not null;
create unique index payments_one_open_per_order_idx
    on public.payments (order_id) where status in ('initiating', 'initiated');
create index payments_order_id_idx on public.payments (order_id);
create index payments_status_created_idx on public.payments (status, created_at);

create or replace function public.validate_payment_insert()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    v_order_status public.order_status;
    v_order_total integer;
begin
    select status, total_amount
      into v_order_status, v_order_total
      from public.orders
     where id = new.order_id
     for update;
    if not found then
        raise exception 'Payment order does not exist';
    end if;
    if v_order_status <> 'pending_payment' then
        raise exception 'Payments require an order awaiting payment';
    end if;
    if new.amount <> v_order_total then
        raise exception 'Payment amount must equal the order total';
    end if;
    return new;
end;
$$;

create or replace function public.guard_payment_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.status in ('completed', 'failed', 'cancelled') then
        raise exception 'A terminal payment cannot be updated';
    end if;
    if new.order_id is distinct from old.order_id
       or new.idempotency_key is distinct from old.idempotency_key
       or new.amount is distinct from old.amount
       or new.phone_number is distinct from old.phone_number
       or new.stk_phone_number is distinct from old.stk_phone_number
       or new.environment is distinct from old.environment
       or new.created_at is distinct from old.created_at then
        raise exception 'Payment identity and request fields are immutable';
    end if;
    if old.checkout_request_id is not null
       and new.checkout_request_id is distinct from old.checkout_request_id then
        raise exception 'Checkout request ID can only be set once';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'initiating' and new.status in ('initiated', 'failed', 'cancelled', 'timeout'))
        or (old.status = 'initiated' and new.status in ('completed', 'failed', 'cancelled', 'timeout'))
        or (old.status = 'timeout' and new.status = 'completed')
    ) then
        raise exception 'Invalid payment status transition from % to %', old.status, new.status;
    end if;
    return new;
end;
$$;

create trigger payments_validate_insert
before insert on public.payments
for each row execute function public.validate_payment_insert();

create trigger payments_guard_update
before update on public.payments
for each row execute function public.guard_payment_update();

create trigger payments_set_updated_at
before update on public.payments
for each row execute function public.set_updated_at();

create table public.payment_events (
    id uuid primary key default gen_random_uuid(),
    payment_id uuid null references public.payments(id),
    checkout_request_id text null,
    event_type text not null,
    payload jsonb not null,
    created_at timestamptz not null default now(),
    constraint payment_events_type_allowed check (
        event_type in ('stk_request', 'stk_response', 'callback', 'status_query', 'error')
    )
);

create index payment_events_payment_created_idx on public.payment_events (payment_id, created_at);
create index payment_events_checkout_request_idx on public.payment_events (checkout_request_id);

create or replace function public.guard_payment_event_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'Payment events are append-only';
    end if;
    if old.payment_id is null
       and new.payment_id is not null
       and (to_jsonb(new) - 'payment_id') = (to_jsonb(old) - 'payment_id') then
        return new;
    end if;
    raise exception 'Payment events are append-only except for callback matching';
end;
$$;

create trigger payment_events_append_only
before update or delete on public.payment_events
for each row execute function public.guard_payment_event_mutation();
