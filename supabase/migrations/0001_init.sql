-- Soulful Content Engine — initial schema (multi-tenant)
-- Postgres / Supabase. RLS enabled on every table.
-- Access model: team_members maps an auth user to a role; member_client_access
-- lists which clients that member may see. Clients (role='client') see only
-- their own rows via the same mapping.

-- ---------- enums ----------
create type user_role       as enum ('founder','assistant','editor','client');
create type client_status   as enum ('onboarding','active','paused','archived');
create type asset_type      as enum ('image','video','story_screenshot');
create type asset_source    as enum ('instagram_feed','instagram_story','editor_upload','manual');
create type asset_status    as enum ('unsorted','sorted','used','rejected');
create type post_status      as enum ('draft','qc','client_review','approved','scheduled','published','rejected');
create type batch_status    as enum ('generating','review','client_review','approved','scheduled','done');
create type trend_category  as enum ('seasonal','uk_cultural','platform_format','niche');

-- ---------- helper: updated_at ----------
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

-- ---------- clients ----------
create table clients (
  id                uuid primary key default gen_random_uuid(),
  name              text not null,
  slug              text unique not null,
  status            client_status not null default 'onboarding',
  instagram_handle  text,
  meta_connection   jsonb,                       -- OAuth tokens / page ids (encrypted at app layer)
  timezone          text not null default 'Europe/London',
  posting_schedule  jsonb not null default '{"per_day":5,"slots":["08:00","12:00","15:00","18:00","20:00"]}',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create trigger t_clients_updated before update on clients for each row execute function set_updated_at();

-- ---------- team members & per-client access ----------
create table team_members (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  role        user_role not null,
  full_name   text,
  created_at  timestamptz not null default now(),
  unique(user_id)
);

create table member_client_access (
  member_id  uuid not null references team_members(id) on delete cascade,
  client_id  uuid not null references clients(id) on delete cascade,
  primary key (member_id, client_id)
);

-- which client_ids may the current auth user access?
create or replace function auth_client_ids() returns setof uuid language sql stable as $$
  select mca.client_id
  from team_members tm
  join member_client_access mca on mca.member_id = tm.id
  where tm.user_id = auth.uid()
$$;

create or replace function auth_is_staff() returns boolean language sql stable as $$
  select exists (
    select 1 from team_members
    where user_id = auth.uid() and role in ('founder','assistant','editor')
  )
$$;

-- ---------- voice documents ----------
create table voice_documents (
  id               uuid primary key default gen_random_uuid(),
  client_id        uuid not null references clients(id) on delete cascade,
  version          int not null default 1,
  body             text not null,               -- FULL brand-voice doc, never truncated
  sample_captions  text[] not null default '{}',-- 10–15 real captions the client wrote
  banned_words     text[] not null default '{}',
  is_active        boolean not null default true,
  created_at       timestamptz not null default now()
);
create index on voice_documents(client_id) where is_active;

-- ---------- content pillars ----------
create table content_pillars (
  id                 uuid primary key default gen_random_uuid(),
  client_id          uuid not null references clients(id) on delete cascade,
  name               text not null,
  description        text,
  tone_notes         text,
  hashtag_strategy   text,
  created_at         timestamptz not null default now()
);
create index on content_pillars(client_id);

-- ---------- assets ----------
create table assets (
  id            uuid primary key default gen_random_uuid(),
  client_id     uuid not null references clients(id) on delete cascade,
  pillar_id     uuid references content_pillars(id) on delete set null,
  type          asset_type not null,
  source        asset_source not null,
  storage_path  text not null,
  status        asset_status not null default 'unsorted',
  captured_at   timestamptz,
  created_at    timestamptz not null default now()
);
create index on assets(client_id, status);

-- ---------- batches ----------
create table batches (
  id           uuid primary key default gen_random_uuid(),
  client_id    uuid not null references clients(id) on delete cascade,
  week_start   date,
  status       batch_status not null default 'generating',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create trigger t_batches_updated before update on batches for each row execute function set_updated_at();

-- ---------- posts ----------
create table posts (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid not null references clients(id) on delete cascade,
  batch_id       uuid references batches(id) on delete set null,
  pillar_id      uuid references content_pillars(id) on delete set null,
  asset_id       uuid references assets(id) on delete set null,
  caption        text,
  hashtags       text,
  status         post_status not null default 'draft',
  voice_score    int,                            -- 0–100 from the voice-audit pass
  client_comment text,
  scheduled_for  timestamptz,
  published_at   timestamptz,
  meta_post_id   text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index on posts(client_id, status);
create index on posts(batch_id);
create trigger t_posts_updated before update on posts for each row execute function set_updated_at();

-- ---------- weekly briefs ----------
create table weekly_briefs (
  id                  uuid primary key default gen_random_uuid(),
  client_id           uuid not null references clients(id) on delete cascade,
  week_start          date not null,
  creative_direction  text,
  trend_notes         text,
  editor_instructions text,
  created_at          timestamptz not null default now(),
  unique(client_id, week_start)
);

-- ---------- trend items (shared library, not client-scoped) ----------
create table trend_items (
  id             uuid primary key default gen_random_uuid(),
  starts_on      date,
  ends_on        date,
  category       trend_category not null,
  description    text not null,
  relevance_tags text[] not null default '{}',
  created_at     timestamptz not null default now()
);

-- ==================================================================
-- RLS
-- ==================================================================
alter table clients               enable row level security;
alter table team_members          enable row level security;
alter table member_client_access  enable row level security;
alter table voice_documents       enable row level security;
alter table content_pillars       enable row level security;
alter table assets                enable row level security;
alter table batches               enable row level security;
alter table posts                 enable row level security;
alter table weekly_briefs         enable row level security;
alter table trend_items           enable row level security;

-- client-scoped tables: access if the row's client_id is in auth_client_ids()
create policy p_clients_sel on clients for select using (id in (select auth_client_ids()));
create policy p_clients_mod on clients for all    using (auth_is_staff() and id in (select auth_client_ids()))
                                          with check (auth_is_staff() and id in (select auth_client_ids()));

do $$
declare t text;
begin
  foreach t in array array['voice_documents','content_pillars','assets','batches','posts','weekly_briefs']
  loop
    execute format($f$
      create policy p_%1$s_sel on %1$s for select using (client_id in (select auth_client_ids()));
      create policy p_%1$s_mod on %1$s for all
        using (auth_is_staff() and client_id in (select auth_client_ids()))
        with check (auth_is_staff() and client_id in (select auth_client_ids()));
    $f$, t);
  end loop;
end $$;

-- clients may comment/approve on their own posts (narrower update handled at app layer)
create policy p_posts_client_review on posts for update
  using (client_id in (select auth_client_ids()))
  with check (client_id in (select auth_client_ids()));

-- trend library: readable by any staff, writable by staff
create policy p_trends_sel on trend_items for select using (auth_is_staff());
create policy p_trends_mod on trend_items for all using (auth_is_staff()) with check (auth_is_staff());

-- team tables: a user sees their own membership; founders manage all
create policy p_tm_self on team_members for select using (user_id = auth.uid() or auth_is_staff());
create policy p_mca_self on member_client_access for select
  using (member_id in (select id from team_members where user_id = auth.uid()) or auth_is_staff());
