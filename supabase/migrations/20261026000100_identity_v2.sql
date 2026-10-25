-- Identity v2: accounts are identified only by a verified email address.
--
-- Phone numbers leave users and admin_invites; a phone number now exists only on a payment.
-- Farmers and buyers confirm their email with a one time code before the account exists
-- (pending_registrations), and every password sign in can require an emailed code
-- (login_challenges). Codes and tokens are stored only as hashes.
--
-- The database is wiped with supabase db reset --linked before this migration runs, so no data is
-- converted. The guard below refuses to run on a database that already has users or invites.
--
-- Objects touched by name (from 20261008000200_users.sql and
-- 20261016000100_admin_invites_and_password_resets.sql):
--   users: constraint users_phone_number_key (unique), constraint users_phone_number_format,
--          index users_email_lower_unique_idx (partial, replaced by a full unique index)
--   admin_invites: index admin_invites_one_pending_phone_idx, constraint admin_invites_phone_number_check,
--          function guard_admin_invite_update (it compared phone_number, so it is replaced)
-- No view or other function references users.phone_number or admin_invites.phone_number.

-- 1. Guard ---------------------------------------------------------------------------------------

do $$
begin
    if exists (select 1 from public.users) then
        raise exception 'identity v2 requires an empty users table. Run supabase db reset --linked, then apply migrations.';
    end if;
    if exists (select 1 from public.admin_invites) then
        raise exception 'identity v2 requires an empty admin_invites table. Run supabase db reset --linked, then apply migrations.';
    end if;
end;
$$;

-- 2. users ---------------------------------------------------------------------------------------

alter table public.users drop constraint if exists users_phone_number_key;
alter table public.users drop constraint if exists users_phone_number_format;
alter table public.users drop column phone_number;

alter table public.users alter column email set not null;

drop index if exists public.users_email_lower_unique_idx;
create unique index users_email_lower_unique_idx on public.users (lower(email));

-- users_email_format (from the users migration) stays; this adds the lower case rule.
alter table public.users add constraint users_email_lowercase check (email = lower(email));

alter table public.users add column email_verified_at timestamptz not null;

-- There is no email change feature. Rejecting any change to email, and to email_verified_at once it
-- is set, closes account takeover paths through these columns.
create or replace function public.guard_user_identity()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.email is distinct from old.email then
        raise exception 'User email is immutable';
    end if;
    if old.email_verified_at is not null and new.email_verified_at is distinct from old.email_verified_at then
        raise exception 'User email_verified_at cannot change once set';
    end if;
    return new;
end;
$$;

create trigger users_guard_identity
before update on public.users
for each row execute function public.guard_user_identity();

-- 3. admin_invites -------------------------------------------------------------------------------

drop index if exists public.admin_invites_one_pending_phone_idx;
alter table public.admin_invites drop constraint if exists admin_invites_phone_number_check;

-- Same rules as before, without the phone_number comparison.
create or replace function public.guard_admin_invite_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.used_at is not null or old.revoked_at is not null then
        raise exception 'A used or revoked invite cannot be changed';
    end if;
    if new.email is distinct from old.email
        or new.name is distinct from old.name
        or new.created_by is distinct from old.created_by
        or new.created_at is distinct from old.created_at
        or new.expires_at is distinct from old.expires_at then
        raise exception 'Invite identity columns are immutable';
    end if;
    if new.attempts < old.attempts then
        raise exception 'Invite attempts can only increase';
    end if;
    return new;
end;
$$;

alter table public.admin_invites drop column phone_number;

-- 4. pending_registrations -----------------------------------------------------------------------

create table public.pending_registrations (
    id uuid primary key default gen_random_uuid(),
    email text not null check (email = lower(email)),
    name text not null,
    role public.user_role not null check (role in ('farmer', 'buyer')),
    password_hash text not null,
    token_hash text not null unique,
    code_hash text not null,
    code_expires_at timestamptz not null,
    last_sent_at timestamptz not null default now(),
    send_count integer not null default 1 check (send_count between 1 and 5),
    attempts integer not null default 0 check (attempts >= 0),
    locked_at timestamptz,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz
);

-- One open (unused) registration per email.
create unique index pending_registrations_one_open_email_idx
    on public.pending_registrations (email)
    where used_at is null;

create or replace function public.guard_pending_registration_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.used_at is not null then
        raise exception 'A used pending registration cannot be changed';
    end if;
    if new.id is distinct from old.id
        or new.email is distinct from old.email
        or new.role is distinct from old.role
        or new.created_at is distinct from old.created_at then
        raise exception 'Pending registration identity columns are immutable';
    end if;
    if new.attempts < old.attempts then
        raise exception 'Pending registration attempts can only increase';
    end if;
    if old.locked_at is not null and new.locked_at is distinct from old.locked_at then
        raise exception 'A locked pending registration cannot be unlocked';
    end if;
    return new;
end;
$$;

create trigger pending_registrations_guard_update
before update on public.pending_registrations
for each row execute function public.guard_pending_registration_update();

-- Because no update is allowed once used_at is set, used_at can be set only once and never cleared.

-- 5. login_challenges ----------------------------------------------------------------------------

create table public.login_challenges (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id),
    token_hash text not null unique,
    code_hash text not null,
    code_expires_at timestamptz not null,
    last_sent_at timestamptz not null default now(),
    send_count integer not null default 1 check (send_count between 1 and 5),
    attempts integer not null default 0 check (attempts >= 0),
    locked_at timestamptz,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz
);

-- One active (unused) challenge per user; a new sign in marks the previous one used first.
create unique index login_challenges_one_active_user_idx
    on public.login_challenges (user_id)
    where used_at is null;

create index login_challenges_user_created_idx on public.login_challenges (user_id, created_at);

create or replace function public.guard_login_challenge_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if old.used_at is not null then
        raise exception 'A used login challenge cannot be changed';
    end if;
    if new.id is distinct from old.id
        or new.user_id is distinct from old.user_id
        or new.token_hash is distinct from old.token_hash
        or new.created_at is distinct from old.created_at then
        raise exception 'Login challenge identity columns are immutable';
    end if;
    if new.attempts < old.attempts then
        raise exception 'Login challenge attempts can only increase';
    end if;
    if old.locked_at is not null and new.locked_at is distinct from old.locked_at then
        raise exception 'A locked login challenge cannot be unlocked';
    end if;
    return new;
end;
$$;

create trigger login_challenges_guard_update
before update on public.login_challenges
for each row execute function public.guard_login_challenge_update();

-- 6. Security ------------------------------------------------------------------------------------
-- The backend connects as the table owner (row level security does not apply to it). The Data API
-- roles get nothing, and pipeline_writer is granted nothing on these tables.

alter table public.pending_registrations enable row level security;
alter table public.login_challenges enable row level security;
revoke all on table public.pending_registrations from anon, authenticated;
revoke all on table public.login_challenges from anon, authenticated;

revoke all on function public.guard_user_identity() from public, anon, authenticated;
revoke all on function public.guard_admin_invite_update() from public, anon, authenticated;
revoke all on function public.guard_pending_registration_update() from public, anon, authenticated;
revoke all on function public.guard_login_challenge_update() from public, anon, authenticated;

-- 7. Cleanup job ---------------------------------------------------------------------------------
-- Rows older than 2 days are long expired. admin_invites is not touched (its history is kept).
-- password_reset_codes has no triggers, so deletes are permitted and its old rows are removed too.

create or replace function public.cleanup_auth_codes()
returns integer
language plpgsql
set search_path = public
as $$
declare
    removed integer := 0;
    step integer;
begin
    delete from public.pending_registrations where created_at < now() - interval '2 days';
    get diagnostics step = row_count;
    removed := removed + step;
    delete from public.login_challenges where created_at < now() - interval '2 days';
    get diagnostics step = row_count;
    removed := removed + step;
    delete from public.password_reset_codes where created_at < now() - interval '2 days';
    get diagnostics step = row_count;
    removed := removed + step;
    return removed;
end;
$$;

revoke execute on function public.cleanup_auth_codes() from public, anon, authenticated;

-- Remove any prior job with this name before scheduling, so rerunning this file recreates it by name.
do $$
declare
    existing_job record;
begin
    for existing_job in
        select jobid from cron.job where jobname = 'cleanup-auth-codes'
    loop
        perform cron.unschedule(existing_job.jobid);
    end loop;
end;
$$;

select cron.schedule('cleanup-auth-codes', '0 2 * * *', $$select public.cleanup_auth_codes()$$);
