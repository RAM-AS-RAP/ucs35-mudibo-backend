-- Manual check for migration 20261015000100_admin_resolution_event.sql.
-- Run in the Supabase SQL Editor after the migration is pushed. Everything happens inside one
-- transaction that is rolled back, so no rows are left behind. The final select lists one row per
-- check; a check that fails raises an error and stops the script instead.

begin;

create temp table check_results(name text, result text) on commit drop;

-- 1. An admin_action event is accepted.
insert into public.payment_events (payment_id, checkout_request_id, event_type, payload)
values (null, 'verification-admin-action', 'admin_action', '{"action": "verification"}'::jsonb);
insert into check_results values ('1 admin_action accepted', 'PASS');

-- 2. An unknown event type is rejected.
do $$
begin
    begin
        insert into public.payment_events (payment_id, checkout_request_id, event_type, payload)
        values (null, 'verification-unknown', 'not_a_real_type', '{}'::jsonb);
        raise exception 'FAIL: an unknown event type was accepted';
    exception
        when check_violation then
            null;
    end;
end;
$$;
insert into check_results values ('2 unknown event type rejected', 'PASS');

-- 3. Deleting a payment_events row is still rejected.
do $$
begin
    begin
        delete from public.payment_events where checkout_request_id = 'verification-admin-action';
        raise exception 'FAIL: a payment event was deleted';
    exception
        when raise_exception then
            if sqlerrm like 'FAIL:%' then
                raise;
            end if;
    end;
end;
$$;
insert into check_results values ('3 delete still rejected', 'PASS');

-- 4. Setting payment_id from null to a value is still allowed.
do $$
declare
    v_buyer uuid;
    v_farmer uuid;
    v_listing uuid;
    v_order uuid;
    v_payment uuid;
begin
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Farmer', 'verification-event-farmer@example.test', 'not-a-hash', 'farmer', now())
    returning id into v_farmer;
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Buyer', 'verification-event-buyer@example.test', 'not-a-hash', 'buyer', now())
    returning id into v_buyer;
    insert into public.produce_listings (farmer_id, crop, market, quantity_available, price_per_unit)
    values (v_farmer, 'maize', (select market_name from public.market_locations limit 1), 10, 50)
    returning id into v_listing;
    insert into public.orders (reference, listing_id, buyer_id, quantity, unit_price, total_amount, expires_at)
    values ('VERIFY01', v_listing, v_buyer, 1, 50, 50, now() + interval '5 minutes')
    returning id into v_order;
    insert into public.payments (order_id, idempotency_key, phone_number, stk_phone_number, amount)
    values (v_order, 'verification-key', '254700000902', '254700000902', 50)
    returning id into v_payment;

    update public.payment_events
       set payment_id = v_payment
     where checkout_request_id = 'verification-admin-action';
    if not exists (
        select 1 from public.payment_events
         where checkout_request_id = 'verification-admin-action' and payment_id = v_payment
    ) then
        raise exception 'FAIL: payment_id was not set';
    end if;
end;
$$;
insert into check_results values ('4 payment_id null to value allowed', 'PASS');

select * from check_results order by name;

rollback;
