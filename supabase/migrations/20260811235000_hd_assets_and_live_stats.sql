-- Preserve every media variant under one Reveal photo identity, queue HD-only analysis,
-- and expose live bounded operational counters for the workspace.

create table deerid.media_assets (
  id uuid primary key default gen_random_uuid(),
  media_id uuid not null references deerid.media(id) on delete restrict,
  variant text not null check (variant in ('cloud_thumbnail', 'cloud_hd', 'cloud_video', 'sd_original', 'derived')),
  object_path text not null,
  image_sha256 text not null check (image_sha256 ~ '^[0-9a-f]{64}$'),
  image_bytes bigint not null check (image_bytes > 0),
  width integer check (width is null or width > 0),
  height integer check (height is null or height > 0),
  content_type text not null check (content_type in ('image/jpeg', 'image/png')),
  observed_at timestamptz not null default now(),
  unique (media_id, variant, object_path),
  unique (object_path)
);

create index media_assets_media_variant_idx
  on deerid.media_assets (media_id, variant, observed_at desc);

alter table deerid.media_assets enable row level security;
revoke all on table deerid.media_assets from public, anon, authenticated;
grant all on table deerid.media_assets to service_role;

create or replace function deerid.media_assets_are_append_only()
returns trigger language plpgsql
set search_path = pg_catalog, deerid, pg_temp
as $$
begin
  raise exception 'media assets are append-only';
end;
$$;
create trigger media_assets_are_append_only
before update or delete on deerid.media_assets
for each row execute function deerid.media_assets_are_append_only();
revoke all on function deerid.media_assets_are_append_only() from public, anon, authenticated;

create table deerid.hd_analysis_jobs (
  id uuid primary key default gen_random_uuid(),
  media_id uuid not null references deerid.media(id) on delete restrict,
  media_asset_id uuid not null references deerid.media_assets(id) on delete restrict,
  stage text not null check (stage in ('quality', 'age', 'antler_score', 'attributes', 'embedding', 'reid')),
  status text not null default 'pending' check (status in ('pending', 'running', 'succeeded', 'failed', 'skipped')),
  result jsonb,
  error_category text,
  queued_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  unique (media_asset_id, stage)
);
create index hd_analysis_jobs_queue_idx
  on deerid.hd_analysis_jobs (status, queued_at) where status = 'pending';
alter table deerid.hd_analysis_jobs enable row level security;
revoke all on table deerid.hd_analysis_jobs from public, anon, authenticated;
grant all on table deerid.hd_analysis_jobs to service_role;

create or replace function deerid.capture_media_asset_and_queue_hd()
returns trigger language plpgsql
set search_path = pg_catalog, deerid, pg_temp
as $$
declare
  asset_id uuid;
  stage_name text;
begin
  if tg_op = 'UPDATE' and (
    old.variant is distinct from new.variant
    or old.object_path is distinct from new.object_path
    or old.image_sha256 is distinct from new.image_sha256
  ) then
    insert into deerid.media_assets (
      media_id, variant, object_path, image_sha256, image_bytes,
      width, height, content_type, observed_at
    ) values (
      old.id, old.variant, old.object_path, old.image_sha256, old.image_bytes,
      old.width, old.height, old.content_type, coalesce(old.last_seen_at, old.ingested_at)
    ) on conflict (media_id, variant, object_path) do nothing;
  end if;

  insert into deerid.media_assets (
    media_id, variant, object_path, image_sha256, image_bytes,
    width, height, content_type, observed_at
  ) values (
    new.id, new.variant, new.object_path, new.image_sha256, new.image_bytes,
    new.width, new.height, new.content_type, coalesce(new.last_seen_at, now())
  )
  on conflict (media_id, variant, object_path) do nothing
  returning id into asset_id;

  if asset_id is null then
    select id into asset_id from deerid.media_assets
    where media_id = new.id and variant = new.variant and object_path = new.object_path;
  end if;

  if new.variant = 'cloud_hd' or new.hd_photo is true then
    foreach stage_name in array array['quality', 'age', 'antler_score', 'attributes', 'embedding', 'reid'] loop
      insert into deerid.hd_analysis_jobs (media_id, media_asset_id, stage)
      values (new.id, asset_id, stage_name)
      on conflict (media_asset_id, stage) do nothing;
    end loop;
  end if;
  return new;
end;
$$;
revoke all on function deerid.capture_media_asset_and_queue_hd() from public, anon, authenticated;

drop trigger if exists media_capture_asset on deerid.media;
create trigger media_capture_asset
after insert or update of variant, object_path, image_sha256, image_bytes, width, height, content_type
on deerid.media
for each row execute function deerid.capture_media_asset_and_queue_hd();

-- Backfill the currently cataloged representation. Historical thumbnails remain immutable
-- in Storage; future HD transitions preserve both old and new rows transactionally.
insert into deerid.media_assets (
  media_id, variant, object_path, image_sha256, image_bytes,
  width, height, content_type, observed_at
)
select id, variant, object_path, image_sha256, image_bytes,
  width, height, content_type, coalesce(last_seen_at, ingested_at)
from deerid.media
on conflict (media_id, variant, object_path) do nothing;

insert into deerid.hd_analysis_jobs (media_id, media_asset_id, stage)
select a.media_id, a.id, s.stage
from deerid.media_assets a
cross join (values ('quality'), ('age'), ('antler_score'), ('attributes'), ('embedding'), ('reid')) s(stage)
where a.variant = 'cloud_hd'
on conflict (media_asset_id, stage) do nothing;

create or replace function public.deerid_operational_stats()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select jsonb_build_object(
    'photos_received_24h', (select count(*)::integer from deerid.media where ingested_at >= now() - interval '24 hours'),
    'hd_requests_24h', (select count(*)::integer from deerid.hd_requests where created_at >= now() - interval '24 hours'),
    'hd_available_24h', (select count(distinct media_id)::integer from deerid.media_assets where variant = 'cloud_hd' and observed_at >= now() - interval '24 hours'),
    'as_of', now()
  );
$$;
revoke all on function public.deerid_operational_stats() from public, anon, authenticated;
grant execute on function public.deerid_operational_stats() to service_role;
