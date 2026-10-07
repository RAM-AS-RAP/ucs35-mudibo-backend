-- Expire pending orders, time out open payments, and restore listing stock.
create or replace function public.expire_stale_orders()
returns integer
language plpgsql
set search_path = public
as $$
declare
    expired_count integer;
begin
    with expired as (
        update public.orders as o
           set status = 'expired'
         where o.status = 'pending_payment'
           and o.expires_at <= now()
        returning o.id, o.listing_id, o.quantity
    ), timed_out as (
        update public.payments as p
           set status = 'timeout',
               result_desc = coalesce(p.result_desc, 'Order expired before payment completed')
         where p.order_id in (select e.id from expired as e)
           and p.status in ('initiating', 'initiated')
        returning p.id
    ), agg as (
        select e.listing_id, sum(e.quantity) as quantity_to_restore
        from expired as e
        group by e.listing_id
    ), restored as (
        update public.produce_listings as l
           set quantity_available = l.quantity_available + a.quantity_to_restore,
               status = case when l.status = 'sold_out' then 'active'::public.listing_status else l.status end
          from agg as a
         where l.id = a.listing_id
        returning l.id
    )
    select count(*)::integer into expired_count from expired;

    return expired_count;
end;
$$;

revoke execute on function public.expire_stale_orders() from public, anon, authenticated;

-- Remove any prior job with this name before scheduling, so rerunning this file recreates it by name.
do $$
declare
    existing_job record;
begin
    for existing_job in
        select jobid from cron.job where jobname = 'expire-stale-orders'
    loop
        perform cron.unschedule(existing_job.jobid);
    end loop;
end;
$$;

select cron.schedule('expire-stale-orders', '* * * * *', $$select public.expire_stale_orders()$$);

-- STK Push Query fallback for missing callbacks runs in the backend, not here, because the database cannot call Daraja.
