-- User accounts and database-enforced account protections.
create table public.users (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    phone_number text not null unique,
    email text null,
    password_hash text not null,
    role public.user_role not null,
    is_super_admin boolean not null default false,
    must_change_password boolean not null default false,
    is_disabled boolean not null default false,
    disabled_at timestamptz null,
    disabled_by uuid null references public.users(id),
    created_by uuid null references public.users(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint users_phone_number_format check (phone_number ~ '^254[17][0-9]{8}$'),
    constraint users_email_format check (email is null or email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    constraint users_id_role_key unique (id, role),
    constraint users_super_admin_role check (not is_super_admin or role = 'admin'),
    constraint users_disabled_timestamp check (is_disabled = (disabled_at is not null))
);

create unique index users_email_lower_unique_idx on public.users (lower(email)) where email is not null;
create unique index users_one_super_admin_idx on public.users (is_super_admin) where is_super_admin;

create or replace function public.guard_user_update()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.role is distinct from old.role then
        raise exception 'User role is immutable';
    end if;
    if old.is_super_admin and (new.is_disabled or not new.is_super_admin or new.role <> 'admin') then
        raise exception 'The super administrator cannot be disabled, demoted, or removed from the admin role';
    end if;
    return new;
end;
$$;

create or replace function public.prevent_user_delete()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    raise exception 'Users cannot be deleted; disable the account instead';
end;
$$;

create trigger users_guard_update
before update on public.users
for each row execute function public.guard_user_update();

create trigger users_set_updated_at
before update on public.users
for each row execute function public.set_updated_at();

create trigger users_prevent_delete
before delete on public.users
for each row execute function public.prevent_user_delete();
