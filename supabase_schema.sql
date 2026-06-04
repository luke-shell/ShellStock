-- Run this once in Supabase SQL Editor
create table if not exists public.shellstock_user_state (
    user_id uuid not null references auth.users(id) on delete cascade,
    data_key text not null,
    data_value jsonb not null,
    updated_at timestamptz not null default now(),
    primary key (user_id, data_key)
);

-- Keep updated_at fresh on updates.
create or replace function public.set_shellstock_user_state_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists shellstock_user_state_set_updated_at on public.shellstock_user_state;
create trigger shellstock_user_state_set_updated_at
before update on public.shellstock_user_state
for each row
execute function public.set_shellstock_user_state_updated_at();

alter table public.shellstock_user_state enable row level security;

drop policy if exists "Users can read their own shellstock state" on public.shellstock_user_state;
create policy "Users can read their own shellstock state"
on public.shellstock_user_state
for select
using (auth.uid() = user_id);

drop policy if exists "Users can insert their own shellstock state" on public.shellstock_user_state;
create policy "Users can insert their own shellstock state"
on public.shellstock_user_state
for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can update their own shellstock state" on public.shellstock_user_state;
create policy "Users can update their own shellstock state"
on public.shellstock_user_state
for update
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own shellstock state" on public.shellstock_user_state;
create policy "Users can delete their own shellstock state"
on public.shellstock_user_state
for delete
using (auth.uid() = user_id);
