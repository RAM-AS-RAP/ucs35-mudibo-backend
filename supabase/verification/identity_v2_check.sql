-- Manual check for migration 20261026000100_identity_v2.sql.
-- Run in the Supabase SQL Editor (or psql) after the migration is applied. Everything happens inside
-- one transaction that is rolled back, so no rows are left behind. The final select lists one PASS row
-- per check; a check that fails raises an error and stops the script instead.
--
-- Note: the migration's guard (it refuses to run when users or admin_invites has rows) is exercised by
-- applying the migration to a populated database, which is not tested here. The database tests run the
-- guard's body against a populated users table inside a savepoint.

begin;

create temp table check_results(name text, result text) on commit drop;
create temp table check_ids(key text primary key, id uuid) on commit drop;

-- Test users, created inside this transaction only.
do $$
declare
    v_user uuid;
    v_other uuid;
begin
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Farmer', 'identity-check-farmer@example.test', 'not-a-hash', 'farmer', now())
    returning id into v_user;
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Buyer', 'identity-check-buyer@example.test', 'not-a-hash', 'buyer', now())
    returning id into v_other;
    insert into check_ids values ('user', v_user), ('other', v_other);
end;
$$;

-- users -------------------------------------------------------------------------------------------

do $$
begin
    begin
        insert into public.users (name, password_hash, role, email_verified_at)
        values ('No Email', 'not-a-hash', 'buyer', now());
        raise exception 'FAIL: a user without an email was accepted';
    exception when not_null_violation then
        null;
    end;
end;
$$;
insert into check_results values ('users 01 a user without an email is rejected', 'PASS');

do $$
begin
    begin
        insert into public.users (name, email, password_hash, role, email_verified_at)
        values ('Upper Email', 'Identity-Check-Upper@example.test', 'not-a-hash', 'buyer', now());
        raise exception 'FAIL: an uppercase email was accepted';
    exception when check_violation then
        null;
    end;
end;
$$;
insert into check_results values ('users 02 an uppercase email is rejected', 'PASS');

do $$
begin
    begin
        -- The lower case check would also reject this row, so the unique index is checked on lower(email)
        -- directly: the same address in another case collides with the existing row.
        perform 1 from pg_indexes
         where schemaname = 'public' and tablename = 'users' and indexname = 'users_email_lower_unique_idx'
           and indexdef ilike '%unique%lower(email)%' and indexdef not ilike '%where%';
        if not found then
            raise exception 'FAIL: users_email_lower_unique_idx is not a full unique index on lower(email)';
        end if;
        insert into public.users (name, email, password_hash, role, email_verified_at)
        values ('Duplicate', 'identity-check-farmer@example.test', 'not-a-hash', 'buyer', now());
        raise exception 'FAIL: a duplicate email was accepted';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('users 03 a duplicate email in different case is rejected', 'PASS');

do $$
begin
    -- The guard was a one time do block: no function carries its text and inserts keep working.
    if exists (select 1 from pg_proc where prosrc ilike '%identity v2 requires%') then
        raise exception 'FAIL: the identity v2 guard exists as a function and could run again';
    end if;
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Guard Check', 'identity-check-guard@example.test', 'not-a-hash', 'buyer', now());
end;
$$;
insert into check_results values ('users 04 the users guard function is not re-run', 'PASS');

do $$
begin
    begin
        update public.users set email = 'identity-check-changed@example.test'
         where id = (select id from check_ids where key = 'user');
        raise exception 'FAIL: email was changed';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    begin
        update public.users set email_verified_at = now() - interval '1 day'
         where id = (select id from check_ids where key = 'user');
        raise exception 'FAIL: email_verified_at was changed';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
end;
$$;
insert into check_results values ('users 05 changing email or email_verified_at fails', 'PASS');

do $$
begin
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name in ('users', 'admin_invites') and column_name = 'phone_number'
    ) then
        raise exception 'FAIL: phone_number still exists on users or admin_invites';
    end if;
end;
$$;
insert into check_results values ('users 06 phone_number is gone from users and admin_invites', 'PASS');

-- pending_registrations ---------------------------------------------------------------------------

do $$
declare
    v_id uuid;
begin
    insert into public.pending_registrations
        (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)
    values ('identity-check-pending@example.test', 'Pending', 'farmer', 'hash', 'token-1', 'code-1',
            now() + interval '15 minutes', now() + interval '60 minutes')
    returning id into v_id;
    insert into check_ids values ('pending', v_id);
    begin
        insert into public.pending_registrations
            (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)
        values ('identity-check-pending@example.test', 'Pending 2', 'farmer', 'hash', 'token-2', 'code-2',
                now() + interval '15 minutes', now() + interval '60 minutes');
        raise exception 'FAIL: two open pending registrations for one email were accepted';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('pending 01 two pending registrations for one email are rejected', 'PASS');

do $$
begin
    begin
        update public.pending_registrations set send_count = 6 where id = (select id from check_ids where key = 'pending');
        raise exception 'FAIL: send_count 6 was accepted';
    exception when check_violation then
        null;
    end;
end;
$$;
insert into check_results values ('pending 02 send_count 6 is rejected', 'PASS');

do $$
begin
    begin
        insert into public.pending_registrations
            (email, name, role, password_hash, token_hash, code_hash, code_expires_at, expires_at)
        values ('identity-check-admin@example.test', 'Admin', 'admin', 'hash', 'token-3', 'code-3',
                now() + interval '15 minutes', now() + interval '60 minutes');
        raise exception 'FAIL: a pending admin registration was accepted';
    exception when check_violation then
        null;
    end;
end;
$$;
insert into check_results values ('pending 03 role admin is rejected', 'PASS');

do $$
begin
    update public.pending_registrations set attempts = 2 where id = (select id from check_ids where key = 'pending');
    begin
        update public.pending_registrations set attempts = 1 where id = (select id from check_ids where key = 'pending');
        raise exception 'FAIL: pending registration attempts decreased';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
end;
$$;
insert into check_results values ('pending 04 attempts cannot decrease', 'PASS');

do $$
begin
    update public.pending_registrations set used_at = now() where id = (select id from check_ids where key = 'pending');
    begin
        update public.pending_registrations set name = 'Changed' where id = (select id from check_ids where key = 'pending');
        raise exception 'FAIL: a used pending registration was updated';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    begin
        update public.pending_registrations set used_at = null where id = (select id from check_ids where key = 'pending');
        raise exception 'FAIL: used_at was cleared';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
end;
$$;
insert into check_results values ('pending 05 a used row cannot be updated', 'PASS');

-- login_challenges --------------------------------------------------------------------------------

do $$
declare
    v_id uuid;
begin
    insert into public.login_challenges (user_id, token_hash, code_hash, code_expires_at, expires_at)
    values ((select id from check_ids where key = 'user'), 'challenge-token-1', 'code-1',
            now() + interval '10 minutes', now() + interval '10 minutes')
    returning id into v_id;
    insert into check_ids values ('challenge', v_id);
    begin
        insert into public.login_challenges (user_id, token_hash, code_hash, code_expires_at, expires_at)
        values ((select id from check_ids where key = 'user'), 'challenge-token-2', 'code-2',
                now() + interval '10 minutes', now() + interval '10 minutes');
        raise exception 'FAIL: two active login challenges for one user were accepted';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('login 01 two active login challenges for one user are rejected', 'PASS');

do $$
begin
    update public.login_challenges set attempts = 3 where id = (select id from check_ids where key = 'challenge');
    begin
        update public.login_challenges set attempts = 0 where id = (select id from check_ids where key = 'challenge');
        raise exception 'FAIL: login challenge attempts decreased';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    begin
        update public.login_challenges set send_count = 6 where id = (select id from check_ids where key = 'challenge');
        raise exception 'FAIL: login challenge send_count 6 was accepted';
    exception when check_violation then
        null;
    end;
end;
$$;
insert into check_results values ('login 02 attempts cannot decrease', 'PASS');

do $$
begin
    update public.login_challenges set used_at = now() where id = (select id from check_ids where key = 'challenge');
    begin
        update public.login_challenges set code_hash = 'changed' where id = (select id from check_ids where key = 'challenge');
        raise exception 'FAIL: a used login challenge was updated';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
end;
$$;
insert into check_results values ('login 03 a used row cannot be updated', 'PASS');

-- Security and jobs -------------------------------------------------------------------------------

do $$
begin
    if exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relname in ('pending_registrations', 'login_challenges')
           and not c.relrowsecurity
    ) then
        raise exception 'FAIL: row level security is not enabled on both tables';
    end if;
    if has_table_privilege('anon', 'public.pending_registrations', 'select, insert, update, delete')
        or has_table_privilege('anon', 'public.login_challenges', 'select, insert, update, delete')
        or has_table_privilege('authenticated', 'public.pending_registrations', 'select, insert, update, delete')
        or has_table_privilege('authenticated', 'public.login_challenges', 'select, insert, update, delete') then
        raise exception 'FAIL: anon or authenticated has a privilege on an auth code table';
    end if;
    if exists (select 1 from pg_roles where rolname = 'pipeline_writer')
        and (has_table_privilege('pipeline_writer', 'public.pending_registrations', 'select, insert, update, delete')
             or has_table_privilege('pipeline_writer', 'public.login_challenges', 'select, insert, update, delete')) then
        raise exception 'FAIL: pipeline_writer has a privilege on an auth code table';
    end if;
end;
$$;
insert into check_results values ('security 01 row level security on and anon has no privilege', 'PASS');

do $$
declare
    v_count integer;
begin
    select count(*) into v_count from cron.job where jobname = 'cleanup-auth-codes';
    if v_count <> 1 then
        raise exception 'FAIL: cron.job contains cleanup-auth-codes % times', v_count;
    end if;
end;
$$;
insert into check_results values ('jobs 01 cleanup-auth-codes is scheduled exactly once', 'PASS');

select * from check_results order by name;

rollback;
