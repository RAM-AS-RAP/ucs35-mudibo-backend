-- Run manually after migrations. The transaction is always rolled back on success.
begin;

do $$
declare
    v_super_admin_id uuid;
    v_farmer_id uuid := gen_random_uuid();
    v_buyer_id uuid := gen_random_uuid();
    v_active_listing_id uuid := gen_random_uuid();
    v_sold_out_listing_id uuid := gen_random_uuid();
    v_payment_order_id uuid := gen_random_uuid();
    v_mismatch_order_id uuid := gen_random_uuid();
    v_timeout_order_id uuid := gen_random_uuid();
    v_expiry_order_id uuid := gen_random_uuid();
    v_payment_id uuid := gen_random_uuid();
    v_timeout_payment_id uuid := gen_random_uuid();
    v_expiry_payment_id uuid := gen_random_uuid();
    v_event_id uuid := gen_random_uuid();
    v_forecast_latest date;
    v_latest_row_count bigint;
    v_expired_count integer;
    v_failed boolean;
begin
    select id into v_super_admin_id
    from public.users
    where is_super_admin
    limit 1;

    if v_super_admin_id is null then
        v_super_admin_id := gen_random_uuid();
        insert into public.users (id, name, email, password_hash, role, is_super_admin, email_verified_at)
        values (
            v_super_admin_id,
            'Schema verification super admin',
            'verification-' || v_super_admin_id::text || '@example.invalid',
            'verification-only-placeholder-hash',
            'admin',
            true,
            now()
        );
    end if;

    insert into public.users (id, name, email, password_hash, role, email_verified_at)
    values (
        v_farmer_id,
        'Schema verification farmer',
        'verification-' || v_farmer_id::text || '@example.invalid',
        'verification-only-placeholder-hash',
        'farmer',
        now()
    );

    insert into public.users (id, name, email, password_hash, role, email_verified_at)
    values (
        v_buyer_id,
        'Schema verification buyer',
        'verification-' || v_buyer_id::text || '@example.invalid',
        'verification-only-placeholder-hash',
        'buyer',
        now()
    );

    v_failed := false;
    begin
        insert into public.users (name, email, password_hash, role, is_super_admin, email_verified_at)
        values (
            'Second verification super admin',
            'verification-second-' || gen_random_uuid()::text || '@example.invalid',
            'verification-only-placeholder-hash',
            'admin',
            true,
            now()
        );
    exception when unique_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: a second super admin was accepted';
    end if;
    raise notice 'PASS: a second super admin is rejected';

    v_failed := false;
    begin
        update public.users
        set is_disabled = true, disabled_at = now()
        where id = v_super_admin_id;
    exception when raise_exception then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: the super admin could be disabled';
    end if;
    raise notice 'PASS: the super admin cannot be disabled';

    v_failed := false;
    begin
        insert into public.produce_listings (
            farmer_id, farmer_role, crop, market, quantity_available, price_per_unit, status
        ) values (
            v_buyer_id, 'farmer', 'maize', 'Nairobi', 1, 10, 'active'
        );
    exception when foreign_key_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: a buyer was accepted as a listing farmer';
    end if;
    raise notice 'PASS: listing farmer role is enforced by the composite foreign key';

    insert into public.produce_listings (
        id, farmer_id, farmer_role, crop, market, quantity_available, price_per_unit, status
    ) values (
        v_active_listing_id, v_farmer_id, 'farmer', 'maize', 'Nairobi', 10, 50, 'active'
    );
    insert into public.produce_listings (
        id, farmer_id, farmer_role, crop, market, quantity_available, price_per_unit, status
    ) values (
        v_sold_out_listing_id, v_farmer_id, 'farmer', 'beans', 'Nairobi', 0, 40, 'sold_out'
    );

    v_failed := false;
    begin
        insert into public.produce_listings (
            farmer_id, farmer_role, crop, market, quantity_available, price_per_unit, status
        ) values (v_farmer_id, 'farmer', 'maize', 'Nairobi', 0, 10, 'active');
    exception when check_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: an active listing with zero stock was accepted';
    end if;

    v_failed := false;
    begin
        insert into public.produce_listings (
            farmer_id, farmer_role, crop, market, quantity_available, price_per_unit, status
        ) values (v_farmer_id, 'farmer', 'maize', 'Nairobi', 1, 10, 'sold_out');
    exception when check_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: a sold-out listing with stock was accepted';
    end if;
    raise notice 'PASS: active and sold-out stock constraints are enforced';

    insert into public.orders (
        id, reference, listing_id, buyer_id, buyer_role, quantity, unit_price, total_amount, expires_at
    ) values (
        v_payment_order_id, upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)),
        v_active_listing_id, v_buyer_id, 'buyer', 2, 50, 100, now() + interval '5 minutes'
    );
    insert into public.orders (
        id, reference, listing_id, buyer_id, buyer_role, quantity, unit_price, total_amount, expires_at
    ) values (
        v_mismatch_order_id, upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)),
        v_active_listing_id, v_buyer_id, 'buyer', 2, 50, 100, now() + interval '5 minutes'
    );
    insert into public.orders (
        id, reference, listing_id, buyer_id, buyer_role, quantity, unit_price, total_amount, expires_at
    ) values (
        v_timeout_order_id, upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)),
        v_active_listing_id, v_buyer_id, 'buyer', 2, 50, 100, now() + interval '5 minutes'
    );
    insert into public.orders (
        id, reference, listing_id, buyer_id, buyer_role, quantity, unit_price, total_amount, expires_at
    ) values (
        v_expiry_order_id, upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)),
        v_sold_out_listing_id, v_buyer_id, 'buyer', 2, 40, 80, now() - interval '1 minute'
    );

    insert into public.payments (id, order_id, idempotency_key, phone_number, stk_phone_number, amount)
    values (
        v_payment_id, v_payment_order_id, 'verify-' || v_payment_id::text,
        '254712345678', '254712345678', 100
    );

    v_failed := false;
    begin
        insert into public.payments (order_id, idempotency_key, phone_number, stk_phone_number, amount)
        values (
            v_payment_order_id, 'verify-open-' || gen_random_uuid()::text,
            '254712345678', '254712345678', 100
        );
    exception when unique_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: two open payments were accepted for one order';
    end if;
    raise notice 'PASS: only one open payment is allowed per order';

    v_failed := false;
    begin
        insert into public.payments (order_id, idempotency_key, phone_number, stk_phone_number, amount)
        values (
            v_mismatch_order_id, 'verify-mismatch-' || gen_random_uuid()::text,
            '254712345678', '254712345678', 99
        );
    exception when raise_exception then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: a payment amount different from the order total was accepted';
    end if;
    raise notice 'PASS: payment amount must match its order total';

    update public.payments set status = 'initiated' where id = v_payment_id;
    update public.payments set status = 'completed', completed_at = now() where id = v_payment_id;
    v_failed := false;
    begin
        update public.payments set result_desc = 'forbidden update' where id = v_payment_id;
    exception when raise_exception then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: a completed payment was updated';
    end if;

    v_timeout_payment_id := gen_random_uuid();
    insert into public.payments (id, order_id, idempotency_key, phone_number, stk_phone_number, amount, status)
    values (
        v_timeout_payment_id, v_timeout_order_id, 'verify-timeout-' || v_timeout_payment_id::text,
        '254712345678', '254712345678', 100, 'timeout'
    );
    update public.payments set status = 'completed', completed_at = now() where id = v_timeout_payment_id;
    if (select status from public.payments where id = v_timeout_payment_id) <> 'completed' then
        raise exception 'FAIL: a timed-out payment could not become completed';
    end if;
    raise notice 'PASS: completed payments are immutable and timeout may become completed';

    insert into public.payment_events (id, payment_id, checkout_request_id, event_type, payload)
    values (v_event_id, null, 'verification-checkout', 'callback', '{"test": true}'::jsonb);
    v_failed := false;
    begin
        delete from public.payment_events where id = v_event_id;
    exception when raise_exception then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: payment event delete was accepted';
    end if;

    v_failed := false;
    begin
        update public.payment_events set payload = '{"changed": true}'::jsonb where id = v_event_id;
    exception when raise_exception then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: payment event payload update was accepted';
    end if;

    update public.payment_events set payment_id = v_payment_id where id = v_event_id;
    if (select payment_id from public.payment_events where id = v_event_id) <> v_payment_id then
        raise exception 'FAIL: orphan callback could not be matched to a payment';
    end if;
    raise notice 'PASS: payment events are append-only except for orphan callback matching';

    v_failed := false;
    begin
        insert into public.forecasts (
            market, crop, horizon, origin_month, target_month, origin_price,
            predicted_log_change, predicted_price, shap_values
        ) values (
            'Nairobi', 'maize', 'one_month', date '2099-01-01', date '2099-03-01',
            100, 0.1, 110, '{}'::jsonb
        );
    exception when check_violation then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: forecast target month inconsistent with its horizon was accepted';
    end if;
    raise notice 'PASS: forecast target month must match the horizon';

    insert into public.forecasts (
        market, crop, horizon, origin_month, target_month, origin_price,
        predicted_log_change, predicted_price, shap_values
    ) values
        ('Nairobi', 'maize', 'one_month', date '2099-01-01', date '2099-02-01', 100, 0.01, 101, '{}'::jsonb),
        ('Nairobi', 'maize', 'one_month', date '2099-02-01', date '2099-03-01', 101, 0.02, 103, '{}'::jsonb);
        select origin_month, count(*) over ()
            into v_forecast_latest, v_latest_row_count
    from public.latest_forecasts
    where market = 'Nairobi' and crop = 'maize' and horizon = 'one_month';
        if v_forecast_latest <> date '2099-02-01' or v_latest_row_count <> 1 then
        raise exception 'FAIL: latest_forecasts did not select the highest origin_month';
    end if;
    raise notice 'PASS: latest_forecasts returns the highest origin month';

    v_expiry_payment_id := gen_random_uuid();
    insert into public.payments (id, order_id, idempotency_key, phone_number, stk_phone_number, amount, status)
    values (
        v_expiry_payment_id, v_expiry_order_id, 'verify-expiry-' || v_expiry_payment_id::text,
        '254712345678', '254712345678', 80, 'initiated'
    );
    v_expired_count := public.expire_stale_orders();
    if (select status from public.orders where id = v_expiry_order_id) <> 'expired' then
        raise exception 'FAIL: expire_stale_orders did not expire the overdue order';
    end if;
    if (select status from public.payments where id = v_expiry_payment_id) <> 'timeout' then
        raise exception 'FAIL: expire_stale_orders did not time out its open payment';
    end if;
    if (select quantity_available from public.produce_listings where id = v_sold_out_listing_id) <> 2 then
        raise exception 'FAIL: expire_stale_orders did not restore stock';
    end if;
    if (select status from public.produce_listings where id = v_sold_out_listing_id) <> 'active' then
        raise exception 'FAIL: restored sold-out listing did not become active';
    end if;
    if v_expired_count < 1 then
        raise exception 'FAIL: expire_stale_orders returned an incorrect count';
    end if;
    raise notice 'PASS: expiry job expires orders, times out payments, restores stock, and reactivates sold-out listings';
end;
$$;

rollback;
