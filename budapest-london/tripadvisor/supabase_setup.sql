-- Matija & Tündi trip — per-person picks/ratings/notes with cross-device sync.
--
-- Data rows stay owned by the Matija/Tündi auth users. A separate, protected
-- allowlist lets the shared admin account edit those two members' rows without
-- weakening the normal write-own-only policies.

create table if not exists public.picks (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  owner_email text default (auth.jwt() ->> 'email'),
  place_key   text not null,                       -- activity key, or reserved @discover-group:v1|city|group-id
  rating      int check (rating between 0 and 5), -- 0..5 stars
  keep        text,                               -- 'yes' | 'no' | null
  note        text,
  reviewed    boolean not null default false,      -- category list was looked through; separate from visiting/rating
  reviewed_revision text,                         -- inventory fingerprint; stale when a crawl changes the category
  updated_at  timestamptz not null default now(),
  unique (user_id, place_key)
);

-- Upgrade existing projects created before category-level collaboration.
alter table public.picks add column if not exists reviewed boolean;
update public.picks set reviewed = false where reviewed is null;
alter table public.picks alter column reviewed set default false;
alter table public.picks alter column reviewed set not null;
alter table public.picks add column if not exists reviewed_revision text;

create table if not exists public.trip_members (
  person_key  text primary key check (person_key in ('matija', 'tundi')),
  display_name text not null,
  user_id     uuid not null unique references auth.users(id) on delete cascade
);

create table if not exists public.trip_admins (
  user_id uuid primary key references auth.users(id) on delete cascade
);

-- Resolve identities server-side. These are login identifiers, never secrets.
insert into public.trip_members (person_key, display_name, user_id)
select v.person_key, v.display_name, u.id
from (
  values
    ('matija', 'Matija', 'matija@cutetrip.com'),
    ('tundi', 'Tündi', 'tundi@cutetrip.com')
) as v(person_key, display_name, email)
join auth.users u on lower(u.email) = v.email
on conflict (person_key) do update
set display_name = excluded.display_name,
    user_id = excluded.user_id;

do $$
declare
  desired_admin_id uuid;
begin
  if (select count(*) from public.trip_members) <> 2 then
    raise exception 'Both Matija and Tündi auth users must exist before applying trip setup';
  end if;

  select id into desired_admin_id
  from auth.users
  where lower(email) = 'admin@cutetrip.com';

  if desired_admin_id is null then
    raise exception 'The shared trip admin auth user must exist before applying trip setup';
  end if;

  delete from public.trip_admins where user_id <> desired_admin_id;
  insert into public.trip_admins (user_id)
  values (desired_admin_id)
  on conflict (user_id) do nothing;

  if (select count(*) from public.trip_admins) <> 1
     or not exists (
       select 1 from public.trip_admins where user_id = desired_admin_id
     ) then
    raise exception 'Trip admin allowlist does not match the configured account';
  end if;
end
$$;

alter table public.picks enable row level security;
alter table public.trip_members enable row level security;
alter table public.trip_admins enable row level security;

-- Authenticated clients may resolve the two lane owners, but cannot alter them.
revoke all on table public.trip_members from public, anon, authenticated;
grant select on table public.trip_members to authenticated;

-- The admin allowlist is only readable through the narrowly scoped function.
revoke all on table public.trip_admins from public, anon, authenticated;

create or replace function public.is_trip_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.trip_admins a
    where a.user_id = (select auth.uid())
  );
$$;

revoke all on function public.is_trip_admin() from public, anon;
grant execute on function public.is_trip_admin() to authenticated;

create or replace function public.is_trip_member()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.trip_members m
    where m.user_id = (select auth.uid())
  );
$$;

revoke all on function public.is_trip_member() from public, anon;
grant execute on function public.is_trip_member() to authenticated;

drop policy if exists "authenticated read trip members" on public.trip_members;
create policy "authenticated read trip members"
on public.trip_members for select
to authenticated
using (
  (select public.is_trip_member())
  or (select public.is_trip_admin())
);

-- Keep the legacy owner_email correct for the deployed client while identity
-- moves to the immutable user_id/trip_members mapping. Also stamp updates.
create or replace function public.stamp_trip_pick()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE'
     and (new.user_id is distinct from old.user_id
          or new.place_key is distinct from old.place_key) then
    raise exception 'Trip pick identity is immutable';
  end if;

  select u.email into new.owner_email
  from auth.users u
  where u.id = new.user_id;

  if new.owner_email is null then
    raise exception 'Unknown trip member user_id';
  end if;

  new.updated_at := now();
  return new;
end;
$$;

revoke all on function public.stamp_trip_pick() from public, anon, authenticated;

drop trigger if exists stamp_trip_pick on public.picks;
create trigger stamp_trip_pick
before insert or update on public.picks
for each row execute function public.stamp_trip_pick();

drop policy if exists "read all" on public.picks;
drop policy if exists "insert own" on public.picks;
drop policy if exists "update own" on public.picks;
drop policy if exists "delete own" on public.picks;
drop policy if exists "admin insert member picks" on public.picks;
drop policy if exists "admin update member picks" on public.picks;
drop policy if exists "admin delete member picks" on public.picks;

-- Matija and Tündi keep their original write-own-only permissions.
create policy "read all"
on public.picks for select
to authenticated
using (
  (select public.is_trip_member())
  or (select public.is_trip_admin())
);

create policy "insert own"
on public.picks for insert
to authenticated
with check (
  not (select public.is_trip_admin())
  and (select public.is_trip_member())
  and user_id = (select auth.uid())
);

create policy "update own"
on public.picks for update
to authenticated
using (
  not (select public.is_trip_admin())
  and (select public.is_trip_member())
  and user_id = (select auth.uid())
)
with check (
  not (select public.is_trip_admin())
  and (select public.is_trip_member())
  and user_id = (select auth.uid())
);

create policy "delete own"
on public.picks for delete
to authenticated
using (
  not (select public.is_trip_admin())
  and (select public.is_trip_member())
  and user_id = (select auth.uid())
);

-- The shared admin may write only rows belonging to the two registered members.
create policy "admin insert member picks"
on public.picks for insert
to authenticated
with check (
  (select public.is_trip_admin())
  and exists (
    select 1 from public.trip_members m where m.user_id = picks.user_id
  )
);

create policy "admin update member picks"
on public.picks for update
to authenticated
using (
  (select public.is_trip_admin())
  and exists (
    select 1 from public.trip_members m where m.user_id = picks.user_id
  )
)
with check (
  (select public.is_trip_admin())
  and exists (
    select 1 from public.trip_members m where m.user_id = picks.user_id
  )
);

create policy "admin delete member picks"
on public.picks for delete
to authenticated
using (
  (select public.is_trip_admin())
  and exists (
    select 1 from public.trip_members m where m.user_id = picks.user_id
  )
);

-- Live cross-device sync; safe to re-run.
do $$
begin
  alter publication supabase_realtime add table public.picks;
exception when duplicate_object then
  null;
end
$$;
