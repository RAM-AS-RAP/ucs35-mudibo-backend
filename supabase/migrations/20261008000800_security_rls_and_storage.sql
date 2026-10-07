-- Deny direct Data API table access. The backend connects as the table owner.
alter table public.users enable row level security;
alter table public.market_locations enable row level security;
alter table public.market_prices enable row level security;
alter table public.forecasts enable row level security;
alter table public.produce_listings enable row level security;
alter table public.orders enable row level security;
alter table public.payments enable row level security;
alter table public.payment_events enable row level security;

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from public, anon, authenticated;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('listing-images', 'listing-images', true, 5242880, array['image/jpeg', 'image/png'])
on conflict (id) do nothing;
