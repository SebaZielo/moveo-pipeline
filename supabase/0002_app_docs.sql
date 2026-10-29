-- MOVEO pipeline: the app's working database, moved off Claude.
-- Run once in Supabase > SQL Editor (safe to run again).
--
-- One table holds every document the Claude app database used to hold, under the same names:
--   events, reactions, settings, harvest, directory, dirgeo, locations, future, runs
-- data is the document exactly as before (the same JSON fields), so the existing scripts work unchanged.
-- The website keeps reading its own tables (events, venues, ...); the daily pipeline fills them from here.

create table if not exists public.app_docs (
  collection  text        not null,
  id          text        not null,
  data        jsonb       not null,
  updated_at  timestamptz not null default now(),
  primary key (collection, id)
);

create index if not exists app_docs_collection_updated on public.app_docs (collection, updated_at desc);

-- Only the server key (used by the pipeline) may read or write. No public access.
alter table public.app_docs enable row level security;

create or replace function public.app_docs_touch() returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists app_docs_touch on public.app_docs;
create trigger app_docs_touch before update on public.app_docs
  for each row execute function public.app_docs_touch();

-- Cover pictures the pipeline finds are stored here (public, so the website can show them).
-- If a "covers" bucket already exists it is left exactly as it is.
insert into storage.buckets (id, name, public)
values ('covers', 'covers', true)
on conflict (id) do nothing;

-- Quick check after a run:
--   select collection, count(*), max(updated_at) from public.app_docs group by 1 order by 1;
