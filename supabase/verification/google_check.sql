-- Manual check for migration 20261027000100_google_sign_in.sql.
-- Run in the Supabase SQL Editor (or psql) after the migration is applied. Everything happens inside
-- one transaction that is rolled back, so no rows are left behind. The final select lists one PASS row
-- per check; a check that fails raises an error and stops the script instead.

begin;

create temp table check_results(name text, result text) on commit drop;
create temp table check_ids(key text primary key, id uuid) on commit drop;

-- Test users, created inside this transaction only.
do $$
declare
    v_user uuid;
    v_other uuid;
    v_google_only uuid;
begin
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Farmer', 'google-check-farmer@example.test', 'not-a-hash', 'farmer', now())
    returning id into v_user;
    insert into public.users (name, email, password_hash, role, email_verified_at)
    values ('Verification Buyer', 'google-check-buyer@example.test', 'not-a-hash', 'buyer', now())
    returning id into v_other;
    insert into public.users (name, email, password_hash, role, email_verified_at, password_set)
    values ('Verification Google', 'google-check-google@example.test', 'not-a-hash', 'buyer', now(), false)
    returning id into v_google_only;
    insert into check_ids values ('user', v_user), ('other', v_other), ('google_only', v_google_only);
end;
$$;

-- users.password_set ------------------------------------------------------------------------------

do $$
begin
    if not (select password_set from public.users where id = (select id from check_ids where key = 'user')) then
        raise exception 'FAIL: password_set does not default to true';
    end if;
    begin
        update public.users set password_set = false where id = (select id from check_ids where key = 'user');
        raise exception 'FAIL: password_set changed from true to false';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    -- false to true is allowed (a password set through the reset flow).
    update public.users set password_set = true where id = (select id from check_ids where key = 'google_only');
    if not (select password_set from public.users where id = (select id from check_ids where key = 'google_only')) then
        raise exception 'FAIL: password_set could not change from false to true';
    end if;
end;
$$;
insert into check_results values ('users 01 password_set defaults to true and cannot go back to false', 'PASS');

-- user_identities ---------------------------------------------------------------------------------

do $$
declare
    v_id uuid;
begin
    insert into public.user_identities (user_id, provider, subject, email, email_verified, name)
    values ((select id from check_ids where key = 'user'), 'google', 'google-check-sub-1',
            'google-check-farmer@example.test', true, 'Farmer')
    returning id into v_id;
    insert into check_ids values ('identity', v_id);
    begin
        insert into public.user_identities (user_id, provider, subject, email, email_verified)
        values ((select id from check_ids where key = 'user'), 'google', 'google-check-sub-2',
                'google-check-farmer@example.test', true);
        raise exception 'FAIL: a second Google identity for one user was accepted';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('identities 01 a second identity for the same user and provider is rejected', 'PASS');

do $$
begin
    begin
        insert into public.user_identities (user_id, provider, subject, email, email_verified)
        values ((select id from check_ids where key = 'other'), 'google', 'google-check-sub-1',
                'google-check-buyer@example.test', true);
        raise exception 'FAIL: one Google subject was linked to two users';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('identities 02 the same provider and subject for two users is rejected', 'PASS');

do $$
begin
    begin
        insert into public.user_identities (user_id, provider, subject, email, email_verified)
        values ((select id from check_ids where key = 'other'), 'google', 'google-check-sub-3',
                'Google-Check-Buyer@example.test', true);
        raise exception 'FAIL: an uppercase identity email was accepted';
    exception when check_violation then
        null;
    end;
    begin
        insert into public.user_identities (user_id, provider, subject, email, email_verified)
        values ((select id from check_ids where key = 'other'), 'google', 'google-check-sub-4',
                'google-check-buyer@example.test', false);
        raise exception 'FAIL: an identity with email_verified false was accepted';
    exception when check_violation then
        null;
    end;
    begin
        insert into public.user_identities (user_id, provider, subject, email, email_verified)
        values ((select id from check_ids where key = 'other'), 'github', 'google-check-sub-5',
                'google-check-buyer@example.test', true);
        raise exception 'FAIL: a provider other than google was accepted';
    exception when check_violation then
        null;
    end;
end;
$$;
insert into check_results values ('identities 03 an uppercase email or email_verified false is rejected', 'PASS');

do $$
declare
    v_change text;
begin
    foreach v_change in array array[
        'user_id = (select id from check_ids where key = ''other'')',
        'subject = ''google-check-changed''',
        'email = ''google-check-changed@example.test''',
        'created_at = now() - interval ''1 day'''
    ]
    loop
        begin
            execute 'update public.user_identities set ' || v_change
                || ' where id = (select id from check_ids where key = ''identity'')';
            raise exception 'FAIL: an immutable identity column changed (%)', v_change;
        exception when raise_exception then
            if sqlerrm like 'FAIL:%' then
                raise;
            end if;
        end;
    end loop;
    -- name and last_login_at may change.
    update public.user_identities set name = 'Renamed', last_login_at = now()
     where id = (select id from check_ids where key = 'identity');
end;
$$;
insert into check_results values ('identities 04 immutable identity columns cannot change', 'PASS');

do $$
declare
    v_google_identity uuid;
begin
    -- A user without a password: the identity is the only sign in method.
    insert into public.users (name, email, password_hash, role, email_verified_at, password_set)
    values ('Verification Google 2', 'google-check-google2@example.test', 'not-a-hash', 'farmer', now(), false);
    insert into public.user_identities (user_id, provider, subject, email, email_verified)
    values ((select id from public.users where email = 'google-check-google2@example.test'), 'google',
            'google-check-sub-6', 'google-check-google2@example.test', true)
    returning id into v_google_identity;
    begin
        delete from public.user_identities where id = v_google_identity;
        raise exception 'FAIL: the only sign in method of a user without a password was deleted';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    -- With a password the identity may be removed.
    delete from public.user_identities where id = (select id from check_ids where key = 'identity');
    if exists (select 1 from public.user_identities where id = (select id from check_ids where key = 'identity')) then
        raise exception 'FAIL: deleting the identity of a user with a password did not work';
    end if;
end;
$$;
insert into check_results values ('identities 05 deleting is refused without a password and allowed with one', 'PASS');

-- oauth_nonces ------------------------------------------------------------------------------------

do $$
declare
    v_id uuid;
begin
    insert into public.oauth_nonces (nonce_hash, expires_at)
    values ('google-check-nonce-1', now() + interval '5 minutes')
    returning id into v_id;
    begin
        update public.oauth_nonces set nonce_hash = 'changed' where id = v_id;
        raise exception 'FAIL: nonce_hash changed';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    update public.oauth_nonces set used_at = now() where id = v_id;
    begin
        update public.oauth_nonces set used_at = null where id = v_id;
        raise exception 'FAIL: a used nonce was made unused';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    begin
        insert into public.oauth_nonces (nonce_hash, expires_at) values ('google-check-nonce-1', now());
        raise exception 'FAIL: a duplicate nonce hash was accepted';
    exception when unique_violation then
        null;
    end;
end;
$$;
insert into check_results values ('nonces 01 a used nonce cannot be un-used', 'PASS');

-- google_signups ----------------------------------------------------------------------------------

do $$
declare
    v_id uuid;
begin
    insert into public.google_signups (token_hash, google_sub, email, name, expires_at)
    values ('google-check-ticket-1', 'google-check-sub-7', 'google-check-new@example.test', 'New',
            now() + interval '10 minutes')
    returning id into v_id;
    begin
        insert into public.google_signups (token_hash, google_sub, email, name, expires_at)
        values ('google-check-ticket-2', 'google-check-sub-7', 'google-check-new@example.test', 'New',
                now() + interval '10 minutes');
        raise exception 'FAIL: two open tickets for one Google subject were accepted';
    exception when unique_violation then
        null;
    end;
    begin
        update public.google_signups set email = 'google-check-other@example.test' where id = v_id;
        raise exception 'FAIL: a ticket column changed';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    update public.google_signups set used_at = now() where id = v_id;
    begin
        update public.google_signups set used_at = null where id = v_id;
        raise exception 'FAIL: a used ticket was made unused';
    exception when raise_exception then
        if sqlerrm like 'FAIL:%' then
            raise;
        end if;
    end;
    -- After the first ticket is used, a new open ticket for the subject is accepted.
    insert into public.google_signups (token_hash, google_sub, email, name, expires_at)
    values ('google-check-ticket-3', 'google-check-sub-7', 'google-check-new@example.test', 'New',
            now() + interval '10 minutes');
end;
$$;
insert into check_results values ('signups 01 one open ticket per subject, allowed again after use', 'PASS');

-- Security and jobs -------------------------------------------------------------------------------

do $$
begin
    if exists (
        select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relname in ('user_identities', 'oauth_nonces', 'google_signups')
           and not c.relrowsecurity
    ) then
        raise exception 'FAIL: row level security is not enabled on all three tables';
    end if;
    if has_table_privilege('anon', 'public.user_identities', 'select, insert, update, delete')
        or has_table_privilege('anon', 'public.oauth_nonces', 'select, insert, update, delete')
        or has_table_privilege('anon', 'public.google_signups', 'select, insert, update, delete')
        or has_table_privilege('authenticated', 'public.user_identities', 'select, insert, update, delete')
        or has_table_privilege('authenticated', 'public.oauth_nonces', 'select, insert, update, delete')
        or has_table_privilege('authenticated', 'public.google_signups', 'select, insert, update, delete') then
        raise exception 'FAIL: anon or authenticated has a privilege on a Google table';
    end if;
    if exists (select 1 from pg_roles where rolname = 'pipeline_writer')
        and (has_table_privilege('pipeline_writer', 'public.user_identities', 'select, insert, update, delete')
             or has_table_privilege('pipeline_writer', 'public.oauth_nonces', 'select, insert, update, delete')
             or has_table_privilege('pipeline_writer', 'public.google_signups', 'select, insert, update, delete')) then
        raise exception 'FAIL: pipeline_writer has a privilege on a Google table';
    end if;
end;
$$;
insert into check_results values ('security 01 row level security on and anon has no privilege', 'PASS');

do $$
declare
    v_count integer;
begin
    select count(*) into v_count from cron.job where jobname = 'cleanup-google-auth';
    if v_count <> 1 then
        raise exception 'FAIL: cron.job contains cleanup-google-auth % times', v_count;
    end if;
end;
$$;
insert into check_results values ('jobs 01 cleanup-google-auth is scheduled exactly once', 'PASS');

select * from check_results order by name;

rollback;
