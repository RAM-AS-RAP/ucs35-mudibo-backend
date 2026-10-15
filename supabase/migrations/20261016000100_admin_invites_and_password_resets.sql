-- Admin invitations and password reset codes (Module 6 addendum).
-- Codes are stored only as an HMAC; the plain code is never written to the database.
-- users.password_changed_at lets the API reject access tokens issued before a password change.

alter table public.users add column if not exists password_changed_at timestamptz;

-- Admin invites ----------------------------------------------------------------------------------

create table public.admin_invites (
    id uuid primary key default gen_random_uuid(),
    email text not null check (email = lower(email)),
    name text not null,
    phone_number text not null check (phone_number ~ '^254[17][0-9]{8}$'),
    code_hash text not null,
    code_expires_at timestamptz not null,
    last_sent_at timestamptz not null default now(),
    send_count integer not null default 1 check (send_count between 1 and 5),
    attempts integer not null default 0 check (attempts >= 0),
    locked_at timestamptz,
    created_by uuid not null references public.users(id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz,
    revoked_at timestamptz
);

-- One pending (not used, not revoked) invite per email and per phone number.
create unique index admin_invites_one_pending_email_idx
    on public.admin_invites (email)
    where used_at is null and revoked_at is null;

create unique index admin_invites_one_pending_phone_idx
    on public.admin_invites (phone_number)
    where used_at is null and revoked_at is null;

create index admin_invites_created_at_idx on public.admin_invites (created_at desc);

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
        or new.phone_number is distinct from old.phone_number
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

create trigger admin_invites_guard_update
before update on public.admin_invites
for each row execute function public.guard_admin_invite_update();

-- Because no update is allowed once used_at or revoked_at is set, both columns can be set only
-- once and never cleared.

alter table public.admin_invites enable row level security;
revoke all on table public.admin_invites from anon, authenticated;

-- Password reset codes ---------------------------------------------------------------------------

create table public.password_reset_codes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id),
    code_hash text not null,
    code_expires_at timestamptz not null,
    attempts integer not null default 0 check (attempts >= 0),
    locked_at timestamptz,
    created_at timestamptz not null default now(),
    used_at timestamptz
);

-- One active (not used) code per user; a new request marks the previous code used first.
create unique index password_reset_codes_one_active_idx
    on public.password_reset_codes (user_id)
    where used_at is null;

create index password_reset_codes_user_created_idx
    on public.password_reset_codes (user_id, created_at);

alter table public.password_reset_codes enable row level security;
revoke all on table public.password_reset_codes from anon, authenticated;
