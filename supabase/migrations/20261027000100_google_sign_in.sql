-- Google sign in for farmers and buyers.
--
-- users.password_set is false only for an account created through Google, until its owner sets a
-- password with the emailed reset code. user_identities binds a Google subject to one user (at most
-- one Google identity per user). oauth_nonces holds single use nonces for Google ID tokens, and
-- google_signups holds single use tickets that keep a verified Google identity while the person
-- chooses farmer or buyer. Nonces and tickets are stored only as SHA-256 hex digests.
--
-- The migration is additive and safe on a populated database: existing users get password_set = true
-- and no existing object is changed. Administrators never get a Google identity; that rule is enforced
-- in the API (app/services/google_auth_service.py).

-- 1. users ---------------------------------------------------------------------------------------

alter table public.users add column password_set boolean not null default true;

-- A password can be set (false to true) but never taken away again (true to false).
create or replace function public.guard_user_password_set()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.password_set and not new.password_set then
        raise exception 'users.password_set cannot change from true to false';
    end if;
    return new;
end;
$$;

create trigger users_guard_password_set
before update on public.users
for each row execute function public.guard_user_password_set();

-- 2. user_identities -----------------------------------------------------------------------------

create table public.user_identities (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id),
    provider text not null check (provider = 'google'),
    subject text not null check (char_length(subject) between 1 and 255),
    email text not null check (email = lower(email)),
    email_verified boolean not null check (email_verified),
    name text,
    created_at timestamptz not null default now(),
    last_login_at timestamptz,
    constraint user_identities_provider_subject_key unique (provider, subject),
    constraint user_identities_user_provider_key unique (user_id, provider)
);

create index user_identities_user_id_idx on public.user_identities (user_id);

-- user_id, provider, subject, email and created_at never change; name and last_login_at may.
-- An identity cannot be deleted while it is the only sign in method (the user has no password).
create or replace function public.guard_user_identities_change()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from public.users where id = old.user_id and not password_set) then
            raise exception 'The only sign in method of an account without a password cannot be removed';
        end if;
        return old;
    end if;
    if new.id is distinct from old.id
        or new.user_id is distinct from old.user_id
        or new.provider is distinct from old.provider
        or new.subject is distinct from old.subject
        or new.email is distinct from old.email
        or new.email_verified is distinct from old.email_verified
        or new.created_at is distinct from old.created_at then
        raise exception 'User identity columns are immutable';
    end if;
    return new;
end;
$$;

create trigger user_identities_guard_update
before update on public.user_identities
for each row execute function public.guard_user_identities_change();

create trigger user_identities_guard_delete
before delete on public.user_identities
for each row execute function public.guard_user_identities_change();

-- 3. oauth_nonces --------------------------------------------------------------------------------

create table public.oauth_nonces (
    id uuid primary key default gen_random_uuid(),
    nonce_hash text not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz
);

-- Only used_at may be set, once; a used row cannot be changed at all, so used_at is never cleared.
create or replace function public.guard_oauth_nonce_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.used_at is not null then
        raise exception 'A used nonce cannot be changed';
    end if;
    if new.id is distinct from old.id
        or new.nonce_hash is distinct from old.nonce_hash
        or new.created_at is distinct from old.created_at
        or new.expires_at is distinct from old.expires_at then
        raise exception 'Nonce columns are immutable';
    end if;
    return new;
end;
$$;

create trigger oauth_nonces_guard_update
before update on public.oauth_nonces
for each row execute function public.guard_oauth_nonce_update();

-- 4. google_signups ------------------------------------------------------------------------------

create table public.google_signups (
    id uuid primary key default gen_random_uuid(),
    token_hash text not null unique,
    google_sub text not null,
    email text not null check (email = lower(email)),
    name text,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz
);

-- One open (unused) ticket per Google subject; a new Google sign in marks the previous one used first.
create unique index google_signups_one_open_subject_idx
    on public.google_signups (google_sub)
    where used_at is null;

create or replace function public.guard_google_signup_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.used_at is not null then
        raise exception 'A used signup ticket cannot be changed';
    end if;
    if new.id is distinct from old.id
        or new.token_hash is distinct from old.token_hash
        or new.google_sub is distinct from old.google_sub
        or new.email is distinct from old.email
        or new.name is distinct from old.name
        or new.created_at is distinct from old.created_at
        or new.expires_at is distinct from old.expires_at then
        raise exception 'Signup ticket columns are immutable';
    end if;
    return new;
end;
$$;

create trigger google_signups_guard_update
before update on public.google_signups
for each row execute function public.guard_google_signup_update();

-- 5. Security ------------------------------------------------------------------------------------
-- The backend connects as the table owner (row level security does not apply to it). The Data API
-- roles get nothing and there are no policies; pipeline_writer is granted nothing on these tables.

alter table public.user_identities enable row level security;
alter table public.oauth_nonces enable row level security;
alter table public.google_signups enable row level security;
revoke all on table public.user_identities from anon, authenticated;
revoke all on table public.oauth_nonces from anon, authenticated;
revoke all on table public.google_signups from anon, authenticated;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'pipeline_writer') then
        revoke all on table public.user_identities from pipeline_writer;
        revoke all on table public.oauth_nonces from pipeline_writer;
        revoke all on table public.google_signups from pipeline_writer;
    end if;
end;
$$;

revoke all on function public.guard_user_password_set() from public, anon, authenticated;
revoke all on function public.guard_user_identities_change() from public, anon, authenticated;
revoke all on function public.guard_oauth_nonce_update() from public, anon, authenticated;
revoke all on function public.guard_google_signup_update() from public, anon, authenticated;

-- 6. Cleanup job ---------------------------------------------------------------------------------
-- Nonces and signup tickets older than 2 days are long expired. user_identities is never touched.

create or replace function public.cleanup_google_auth()
returns integer
language plpgsql
set search_path = public
as $$
declare
    removed integer := 0;
    step integer;
begin
    delete from public.oauth_nonces where created_at < now() - interval '2 days';
    get diagnostics step = row_count;
    removed := removed + step;
    delete from public.google_signups where created_at < now() - interval '2 days';
    get diagnostics step = row_count;
    removed := removed + step;
    return removed;
end;
$$;

revoke execute on function public.cleanup_google_auth() from public, anon, authenticated;

-- Remove any prior job with this name before scheduling, so rerunning this file recreates it by name.
do $$
declare
    existing_job record;
begin
    for existing_job in
        select jobid from cron.job where jobname = 'cleanup-google-auth'
    loop
        perform cron.unschedule(existing_job.jobid);
    end loop;
end;
$$;

select cron.schedule('cleanup-google-auth', '30 2 * * *', $$select public.cleanup_google_auth()$$);
