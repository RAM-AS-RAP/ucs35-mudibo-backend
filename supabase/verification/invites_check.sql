-- Manual check for migration 20261016000100_admin_invites_and_password_resets.sql.
-- Run in the Supabase SQL Editor after the migration is pushed. Everything happens inside one
-- transaction that is rolled back, so no rows are left behind. The final select lists one PASS row
-- per check; a check that fails raises an error and stops the script instead.

begin;

create temp table check_results(name text, result text) on commit drop;

-- Test users and one pending invite, created inside this transaction only.
create temp table check_ids(key text primary key, id uuid) on commit drop;

do $$
declare
    v_admin uuid;
    v_user uuid;
    v_invite uuid;
begin
    insert into public.users (name, phone_number, email, password_hash, role)
    values ('Verification Admin', '254700000951', 'verification-admin@example.test', 'not-a-hash', 'admin')
    returning id into v_admin;
    insert into public.users (name, phone_number, email, password_hash, role)
    values ('Verification Farmer', '254700000952', 'verification-farmer@example.test', 'not-a-hash', 'farmer')
    returning id into v_user;
    insert into public.admin_invites (email, name, phone_number, code_hash, code_expires_at, created_by, expires_at)
    values (
        'verification-invite@example.test', 'Verification Invite', '254700000953', 'hash-1',
        now() + interval '15 minutes', v_admin, now() + interval '7 days'
    )
    returning id into v_invite;
    insert into check_ids values ('admin', v_admin), ('user', v_user), ('invite', v_invite);
end;
$$;

-- 1. A second pending invite for the same email is rejected.
do $$
begin
    begin
        insert into public.admin_invites (email, name, phone_number, code_hash, code_expires_at, created_by, expires_at)
        values (
            'verification-invite@example.test', 'Second Invite', '254700000954', 'hash-2',
            now() + interval '15 minutes', (select id from check_ids where key = 'admin'), now() + interval '7 days'
        );
        raise exception 'FAIL: a second pending invite for one email was accepted';
    exception
        when unique_violation then
            null;
    end;
end;
$$;
insert into check_results values ('1 second pending invite for one email rejected', 'PASS');

-- 2. send_count of 6 is rejected.
do $$
begin
    begin
        update public.admin_invites set send_count = 6 where id = (select id from check_ids where key = 'invite');
        raise exception 'FAIL: send_count 6 was accepted';
    exception
        when check_violation then
            null;
    end;
end;
$$;
insert into check_results values ('2 send_count 6 rejected', 'PASS');

-- 3. attempts cannot decrease.
do $$
begin
    update public.admin_invites set attempts = 3 where id = (select id from check_ids where key = 'invite');
    begin
        update public.admin_invites set attempts = 2 where id = (select id from check_ids where key = 'invite');
        raise exception 'FAIL: attempts decreased';
    exception
        when raise_exception then
            if sqlerrm like 'FAIL:%' then
                raise;
            end if;
    end;
end;
$$;
insert into check_results values ('3 attempts cannot decrease', 'PASS');

-- 4. An immutable column cannot change.
do $$
begin
    begin
        update public.admin_invites set email = 'changed@example.test'
         where id = (select id from check_ids where key = 'invite');
        raise exception 'FAIL: an immutable invite column changed';
    exception
        when raise_exception then
            if sqlerrm like 'FAIL:%' then
                raise;
            end if;
    end;
end;
$$;
insert into check_results values ('4 immutable column cannot change', 'PASS');

-- 5. A used invite cannot be updated (not even to clear used_at).
do $$
begin
    update public.admin_invites set used_at = now() where id = (select id from check_ids where key = 'invite');
    begin
        update public.admin_invites set used_at = null where id = (select id from check_ids where key = 'invite');
        raise exception 'FAIL: a used invite was updated';
    exception
        when raise_exception then
            if sqlerrm like 'FAIL:%' then
                raise;
            end if;
    end;
end;
$$;
insert into check_results values ('5 used invite cannot be updated', 'PASS');

-- 6. A second active reset code for the same user is rejected.
do $$
begin
    insert into public.password_reset_codes (user_id, code_hash, code_expires_at)
    values ((select id from check_ids where key = 'user'), 'reset-hash-1', now() + interval '15 minutes');
    begin
        insert into public.password_reset_codes (user_id, code_hash, code_expires_at)
        values ((select id from check_ids where key = 'user'), 'reset-hash-2', now() + interval '15 minutes');
        raise exception 'FAIL: a second active reset code was accepted';
    exception
        when unique_violation then
            null;
    end;
end;
$$;
insert into check_results values ('6 second active reset code rejected', 'PASS');

-- 7. Row level security is enabled on both tables.
do $$
begin
    if not (select relrowsecurity from pg_class where oid = 'public.admin_invites'::regclass) then
        raise exception 'FAIL: row level security is off on admin_invites';
    end if;
    if not (select relrowsecurity from pg_class where oid = 'public.password_reset_codes'::regclass) then
        raise exception 'FAIL: row level security is off on password_reset_codes';
    end if;
end;
$$;
insert into check_results values ('7 row level security enabled', 'PASS');

-- 8. The anon role has no privilege on either table.
do $$
declare
    v_table text;
    v_privilege text;
begin
    foreach v_table in array array['public.admin_invites', 'public.password_reset_codes'] loop
        foreach v_privilege in array array['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] loop
            if has_table_privilege('anon', v_table, v_privilege) then
                raise exception 'FAIL: anon has % on %', v_privilege, v_table;
            end if;
        end loop;
    end loop;
end;
$$;
insert into check_results values ('8 anon has no privilege', 'PASS');

select * from check_results order by name;

rollback;
