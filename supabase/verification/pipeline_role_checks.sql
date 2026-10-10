-- Run manually as postgres in the Supabase SQL Editor after the pipeline role migration.
begin;

do $$
declare
    v_count bigint;
    v_price numeric(12, 2);
    v_predicted numeric(12, 2);
    v_table text;
    v_failed boolean;
    v_is_super boolean;
    v_bypass_rls boolean;
begin
    begin
        execute 'set local role pipeline_writer';
    exception when insufficient_privilege then
        raise notice 'SKIP: SET ROLE pipeline_writer is not permitted here; use the manual connection test in README.md instead.';
        return;
    end;

    execute 'select count(*) from public.market_locations' into v_count;
    execute 'select count(*) from public.market_prices' into v_count;
    execute 'select count(*) from public.forecasts' into v_count;
    raise notice 'PASS: pipeline_writer can select from all three pipeline tables';

    execute $query$
        insert into public.market_prices (market, crop, month, avg_price_per_kg, source)
        values ('Nairobi', 'maize', date '2099-01-01', 123.45, 'fews')
        on conflict (market, crop, month) do update
        set avg_price_per_kg = excluded.avg_price_per_kg
    $query$;
    execute $query$
        insert into public.market_prices (market, crop, month, avg_price_per_kg, source)
        values ('Nairobi', 'maize', date '2099-01-01', 124.56, 'fews')
        on conflict (market, crop, month) do update
        set avg_price_per_kg = excluded.avg_price_per_kg
    $query$;
    execute $query$
        select avg_price_per_kg
        from public.market_prices
        where market = 'Nairobi' and crop = 'maize' and month = date '2099-01-01'
    $query$ into v_price;
    if v_price <> 124.56 then
        raise exception 'FAIL: market_prices upsert did not update the test value';
    end if;
    raise notice 'PASS: market_prices insert and ON CONFLICT update succeed';

    execute $query$
        insert into public.forecasts (
            market, crop, horizon, origin_month, target_month, origin_price,
            predicted_log_change, predicted_price, shap_values
        ) values (
            'Nairobi', 'maize', 'one_month', date '2099-01-01', date '2099-02-01',
            123.45, 0.01, 124.69, '{}'::jsonb
        )
        on conflict (market, crop, horizon, origin_month) do update
        set predicted_price = excluded.predicted_price
    $query$;
    execute $query$
        insert into public.forecasts (
            market, crop, horizon, origin_month, target_month, origin_price,
            predicted_log_change, predicted_price, shap_values
        ) values (
            'Nairobi', 'maize', 'one_month', date '2099-01-01', date '2099-02-01',
            123.45, 0.01, 125.70, '{}'::jsonb
        )
        on conflict (market, crop, horizon, origin_month) do update
        set predicted_price = excluded.predicted_price
    $query$;
    execute $query$
        select predicted_price
        from public.forecasts
        where market = 'Nairobi' and crop = 'maize'
          and horizon = 'one_month' and origin_month = date '2099-01-01'
    $query$ into v_predicted;
    if v_predicted <> 125.70 then
        raise exception 'FAIL: forecasts upsert did not update the test value';
    end if;
    raise notice 'PASS: forecasts insert and ON CONFLICT update succeed';

    v_failed := false;
    begin
        execute $query$
            update public.market_prices
            set month = date '2099-03-01'
            where market = 'Nairobi' and crop = 'maize' and month = date '2099-01-01'
        $query$;
    exception when insufficient_privilege then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: pipeline_writer was able to update a market_prices key column';
    end if;
    raise notice 'PASS: market_prices key columns cannot be updated';

    foreach v_table in array array['market_prices', 'forecasts'] loop
        v_failed := false;
        begin
            execute format('delete from public.%I', v_table);
        exception when insufficient_privilege then
            v_failed := true;
        end;
        if not v_failed then
            raise exception 'FAIL: pipeline_writer was able to delete from %', v_table;
        end if;
    end loop;
    raise notice 'PASS: pipeline_writer has no delete privilege on market_prices or forecasts';

    foreach v_table in array array['users', 'produce_listings', 'orders', 'payments', 'payment_events'] loop
        v_failed := false;
        begin
            execute format('select count(*) from public.%I', v_table) into v_count;
        exception when insufficient_privilege then
            v_failed := true;
        end;
        if not v_failed then
            raise exception 'FAIL: pipeline_writer was able to select from %', v_table;
        end if;
    end loop;
    raise notice 'PASS: protected account, listing, order, and payment tables are not readable';

    v_failed := false;
    begin
        execute 'insert into public.orders default values';
    exception when insufficient_privilege then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: pipeline_writer was able to insert into orders';
    end if;

    v_failed := false;
    begin
        execute 'update public.users set name = name';
    exception when insufficient_privilege then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: pipeline_writer was able to update users';
    end if;
    raise notice 'PASS: pipeline_writer cannot insert orders or update users';

    v_failed := false;
    begin
        execute 'create table public.pipeline_writer_forbidden_probe (id integer)';
    exception when insufficient_privilege then
        v_failed := true;
    end;
    if not v_failed then
        raise exception 'FAIL: pipeline_writer was able to create a public table';
    end if;
    raise notice 'PASS: pipeline_writer cannot create objects in public';

    foreach v_table in array array['latest_forecasts', 'dashboard_stats'] loop
        v_failed := false;
        begin
            execute format('select count(*) from public.%I', v_table) into v_count;
        exception when insufficient_privilege then
            v_failed := true;
        end;
        if not v_failed then
            raise exception 'FAIL: pipeline_writer was able to select from view %', v_table;
        end if;
    end loop;
    raise notice 'PASS: pipeline_writer cannot select from public views';

    execute 'reset role';
    select rolsuper, rolbypassrls
      into v_is_super, v_bypass_rls
      from pg_roles
     where rolname = 'pipeline_writer';
    if v_is_super or v_bypass_rls then
        raise exception 'FAIL: pipeline_writer must not be superuser or bypass RLS';
    end if;
    raise notice 'PASS: pipeline_writer is not superuser and does not bypass RLS';
end;
$$;

rollback;
