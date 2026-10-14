-- Allow the admin_action event type in payment_events, used to record how an administrator
-- resolved a needs_reconciliation order. The append-only trigger and all other objects are unchanged.

do $$
declare
    existing record;
begin
    for existing in
        select c.conname
          from pg_constraint as c
         where c.conrelid = 'public.payment_events'::regclass
           and c.contype = 'c'
           and pg_get_constraintdef(c.oid) like '%event_type%'
    loop
        execute format('alter table public.payment_events drop constraint %I', existing.conname);
    end loop;
end;
$$;

alter table public.payment_events
    add constraint payment_events_event_type_check check (
        event_type in ('stk_request', 'stk_response', 'callback', 'status_query', 'error', 'admin_action')
    );
