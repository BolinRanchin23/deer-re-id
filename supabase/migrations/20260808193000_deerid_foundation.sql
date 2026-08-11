-- DeerID private Reveal catalog foundation.
-- Media bytes remain in the private tactacam-photos Storage bucket; this schema
-- contains searchable metadata, provenance, classification state, and collections.

create extension if not exists pgcrypto;
create schema if not exists deerid;

revoke all on schema deerid from public;
revoke all on schema deerid from anon;
revoke all on schema deerid from authenticated;
grant usage on schema deerid to service_role;

create or replace function deerid.try_numeric(value text)
returns numeric
language plpgsql
immutable
strict
set search_path = pg_catalog
as $$
begin
  return value::numeric;
exception when invalid_text_representation or numeric_value_out_of_range then
  return null;
end;
$$;

create or replace function deerid.try_timestamptz(value text)
returns timestamptz
language plpgsql
immutable
strict
set search_path = pg_catalog
as $$
begin
  return value::timestamptz;
exception when invalid_datetime_format or datetime_field_overflow then
  return null;
end;
$$;

create table if not exists deerid.cameras (
  id uuid primary key default gen_random_uuid(),
  provider text not null default 'reveal' check (provider = 'reveal'),
  provider_camera_id text not null,
  provider_account_id text,
  name text,
  location_name text,
  postal_code text,
  hardware_version text,
  firmware_version text,
  firmware_status text,
  plan_name text,
  carrier text,
  activated_at timestamptz,
  warranty_ends_at timestamptz,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  unique (provider, provider_camera_id)
);

create table if not exists deerid.camera_locations (
  id bigint generated always as identity primary key,
  camera_id uuid not null references deerid.cameras(id) on delete cascade,
  latitude numeric(9,6) not null check (latitude between -90 and 90),
  longitude numeric(9,6) not null check (longitude between -180 and 180),
  observed_at timestamptz not null,
  source text not null check (source in ('provider_camera', 'provider_photo', 'user', 'imported', 'inferred')),
  accuracy_meters numeric,
  raw_payload jsonb not null default '{}'::jsonb,
  unique (camera_id, observed_at, source)
);
create index if not exists camera_locations_camera_time_idx
  on deerid.camera_locations (camera_id, observed_at desc);

create table if not exists deerid.camera_status_observations (
  id bigint generated always as identity primary key,
  camera_id uuid not null references deerid.cameras(id) on delete cascade,
  observed_at timestamptz not null,
  battery_level numeric,
  signal_level numeric,
  temperature numeric,
  memory_used numeric,
  memory_limit numeric,
  internal_voltage numeric,
  external_voltage numeric,
  voltage_source text,
  solar_percent numeric,
  sd_card_status text,
  last_transmission_at timestamptz,
  serving_cell text,
  raw_payload jsonb not null default '{}'::jsonb,
  unique (camera_id, observed_at)
);
create index if not exists camera_status_camera_time_idx
  on deerid.camera_status_observations (camera_id, observed_at desc);

create table if not exists deerid.camera_settings_snapshots (
  id bigint generated always as identity primary key,
  camera_id uuid not null references deerid.cameras(id) on delete cascade,
  observed_at timestamptz not null,
  settings jsonb not null,
  unique (camera_id, observed_at)
);

create table if not exists deerid.media (
  id uuid primary key default gen_random_uuid(),
  provider text not null default 'reveal' check (provider = 'reveal'),
  provider_photo_id text not null,
  camera_id uuid references deerid.cameras(id) on delete set null,
  provider_camera_id text not null,
  captured_at timestamptz not null,
  synchronized_at timestamptz,
  media_type text,
  ownership_type text,
  variant text not null default 'cloud_thumbnail'
    check (variant in ('cloud_thumbnail', 'cloud_hd', 'cloud_video', 'sd_original', 'derived')),
  hd_photo boolean,
  has_headshot boolean,
  delay_syncing boolean,
  battery_level numeric,
  signal_level numeric,
  object_path text not null,
  image_sha256 text not null check (image_sha256 ~ '^[0-9a-f]{64}$'),
  image_bytes bigint not null check (image_bytes > 0),
  width integer check (width is null or width > 0),
  height integer check (height is null or height > 0),
  content_type text not null check (content_type in ('image/jpeg', 'image/png')),
  filename text,
  ingested_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  raw_payload jsonb not null,
  unique (provider, provider_photo_id),
  unique (object_path)
);
create index if not exists media_camera_captured_idx
  on deerid.media (camera_id, captured_at desc);
create index if not exists media_captured_idx
  on deerid.media (captured_at desc);

create table if not exists deerid.media_weather (
  media_id uuid primary key references deerid.media(id) on delete cascade,
  observed_at timestamptz,
  condition text,
  temperature numeric,
  pressure numeric,
  pressure_tendency text,
  minimum_temperature_12h numeric,
  maximum_temperature_12h numeric,
  temperature_departure_24h numeric,
  wind_direction_degrees numeric,
  wind_direction_short text,
  wind_direction_long text,
  wind_speed numeric,
  wind_gust numeric,
  moon_phase text,
  sun_phase text,
  raw_payload jsonb not null default '{}'::jsonb
);

create table if not exists deerid.media_labels (
  id uuid primary key default gen_random_uuid(),
  media_id uuid not null references deerid.media(id) on delete cascade,
  namespace text not null default 'species',
  label text not null,
  source text not null check (source in ('model', 'human', 'reveal', 'import')),
  confidence numeric check (confidence is null or confidence between 0 and 1),
  model_name text,
  model_version text,
  status text not null default 'suggested' check (status in ('suggested', 'confirmed', 'rejected')),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (media_id, namespace, label, source, model_name, model_version)
);
create index if not exists media_labels_media_idx on deerid.media_labels (media_id);
create index if not exists media_labels_label_idx on deerid.media_labels (namespace, label);

create table if not exists deerid.animals (
  id uuid primary key default gen_random_uuid(),
  species text not null default 'white-tailed deer',
  display_name text not null,
  sex text,
  life_stage text,
  notes text,
  status text not null default 'active' check (status in ('active', 'archived', 'merged')),
  merged_into_id uuid references deerid.animals(id) on delete set null,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists deerid.animal_profiles (
  id uuid primary key default gen_random_uuid(),
  animal_id uuid not null references deerid.animals(id) on delete cascade,
  season_year integer not null check (season_year between 2000 and 2200),
  appearance_notes text,
  antler_notes text,
  face_notes text,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (animal_id, season_year)
);

create table if not exists deerid.animal_media (
  id uuid primary key default gen_random_uuid(),
  animal_profile_id uuid not null references deerid.animal_profiles(id) on delete cascade,
  media_id uuid not null references deerid.media(id) on delete cascade,
  match_source text not null check (match_source in ('model', 'human', 'import')),
  match_confidence numeric check (match_confidence is null or match_confidence between 0 and 1),
  confirmation_status text not null default 'suggested'
    check (confirmation_status in ('suggested', 'confirmed', 'rejected')),
  confirmed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (animal_profile_id, media_id)
);

create table if not exists deerid.collections (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text,
  collection_type text not null default 'local'
    check (collection_type in ('local', 'reveal_hit_list', 'review_queue', 'share_album')),
  provider_collection_id text,
  visibility text not null default 'private' check (visibility in ('private', 'members', 'public')),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique nulls not distinct (collection_type, provider_collection_id)
);

create table if not exists deerid.collection_items (
  id uuid primary key default gen_random_uuid(),
  collection_id uuid not null references deerid.collections(id) on delete cascade,
  media_id uuid references deerid.media(id) on delete cascade,
  animal_id uuid references deerid.animals(id) on delete cascade,
  position integer,
  notes text,
  added_by uuid references auth.users(id) on delete set null,
  added_at timestamptz not null default now(),
  unique nulls not distinct (collection_id, media_id, animal_id),
  check (num_nonnulls(media_id, animal_id) = 1)
);

create table if not exists deerid.classification_jobs (
  id uuid primary key default gen_random_uuid(),
  media_id uuid not null references deerid.media(id) on delete cascade,
  stage text not null default 'triage' check (stage in ('triage', 'species', 'quality', 'embedding', 'reid')),
  model_name text not null,
  model_version text,
  status text not null default 'pending' check (status in ('pending', 'running', 'succeeded', 'failed', 'skipped')),
  input_variant text not null default 'cloud_thumbnail',
  result jsonb,
  error_category text,
  queued_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  unique nulls not distinct (media_id, stage, model_name, model_version)
);
create index if not exists classification_jobs_queue_idx
  on deerid.classification_jobs (status, queued_at) where status = 'pending';

create table if not exists deerid.ingestion_runs (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'reveal',
  started_at timestamptz not null,
  finished_at timestamptz not null,
  status text not null check (status in ('succeeded', 'degraded', 'failed')),
  camera_count integer not null default 0,
  media_count integer not null default 0,
  details jsonb not null default '{}'::jsonb
);

alter table deerid.cameras enable row level security;
alter table deerid.camera_locations enable row level security;
alter table deerid.camera_status_observations enable row level security;
alter table deerid.camera_settings_snapshots enable row level security;
alter table deerid.media enable row level security;
alter table deerid.media_weather enable row level security;
alter table deerid.media_labels enable row level security;
alter table deerid.animals enable row level security;
alter table deerid.animal_profiles enable row level security;
alter table deerid.animal_media enable row level security;
alter table deerid.collections enable row level security;
alter table deerid.collection_items enable row level security;
alter table deerid.classification_jobs enable row level security;
alter table deerid.ingestion_runs enable row level security;

revoke all on all tables in schema deerid from public, anon, authenticated;
grant all on all tables in schema deerid to service_role;
grant usage, select on all sequences in schema deerid to service_role;

create or replace function public.deerid_ingest_reveal_batch(
  p_cameras jsonb,
  p_media jsonb,
  p_observed_at timestamptz default now()
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  c jsonb;
  item jsonb;
  photo jsonb;
  camera_row deerid.cameras%rowtype;
  media_row deerid.media%rowtype;
  camera_total integer := 0;
  media_total integer := 0;
  lat numeric;
  lon numeric;
  observed timestamptz;
  weather jsonb;
begin
  if jsonb_typeof(coalesce(p_cameras, '[]'::jsonb)) <> 'array'
     or jsonb_typeof(coalesce(p_media, '[]'::jsonb)) <> 'array' then
    raise exception 'catalog payloads must be arrays';
  end if;

  for c in select value from jsonb_array_elements(coalesce(p_cameras, '[]'::jsonb)) loop
    if nullif(c->>'cameraId', '') is null then
      continue;
    end if;

    insert into deerid.cameras (
      provider_camera_id, provider_account_id, name, location_name, postal_code,
      hardware_version, firmware_version, firmware_status, plan_name, carrier,
      activated_at, warranty_ends_at, last_seen_at, raw_payload
    ) values (
      c->>'cameraId', c->>'accountId', c->>'name', c->>'location', coalesce(c->>'zip', c->>'zipCode'),
      c->>'hardwareVersion', coalesce(c->>'firmwareVersion', c#>>'{status,firmwareVersion}'),
      c->>'firmwareStatus', coalesce(c->>'planName', c->>'plan'), coalesce(c->>'carrier', c#>>'{status,carrier}'),
      deerid.try_timestamptz(c->>'firstActivationTime'), deerid.try_timestamptz(c->>'warrantyEndDate'),
      p_observed_at, c
    )
    on conflict (provider, provider_camera_id) do update set
      provider_account_id = excluded.provider_account_id,
      name = excluded.name,
      location_name = excluded.location_name,
      postal_code = excluded.postal_code,
      hardware_version = excluded.hardware_version,
      firmware_version = excluded.firmware_version,
      firmware_status = excluded.firmware_status,
      plan_name = excluded.plan_name,
      carrier = excluded.carrier,
      activated_at = coalesce(excluded.activated_at, deerid.cameras.activated_at),
      warranty_ends_at = coalesce(excluded.warranty_ends_at, deerid.cameras.warranty_ends_at),
      last_seen_at = excluded.last_seen_at,
      raw_payload = excluded.raw_payload
    returning * into camera_row;
    camera_total := camera_total + 1;

    observed := coalesce(
      deerid.try_timestamptz(c#>>'{gps,lastUpdatedTimestamp}'),
      deerid.try_timestamptz(c->>'updatedAt'),
      p_observed_at
    );
    lat := deerid.try_numeric(coalesce(c#>>'{gps,latitude}', c#>>'{gps,lat}'));
    lon := deerid.try_numeric(coalesce(c#>>'{gps,longitude}', c#>>'{gps,lon}'));
    if lat between -90 and 90 and lon between -180 and 180 then
      insert into deerid.camera_locations (camera_id, latitude, longitude, observed_at, source, raw_payload)
      values (camera_row.id, lat, lon, observed, 'provider_camera', coalesce(c->'gps', '{}'::jsonb))
      on conflict (camera_id, observed_at, source) do update set
        latitude = excluded.latitude, longitude = excluded.longitude, raw_payload = excluded.raw_payload;
    end if;

    if jsonb_typeof(c->'status') = 'object' then
      observed := coalesce(
        deerid.try_timestamptz(c#>>'{status,lastTransmissionTime}'),
        deerid.try_timestamptz(c->>'updatedAt'),
        p_observed_at
      );
      insert into deerid.camera_status_observations (
        camera_id, observed_at, battery_level, signal_level, temperature,
        memory_used, memory_limit, internal_voltage, external_voltage, voltage_source,
        solar_percent, sd_card_status, last_transmission_at, serving_cell, raw_payload
      ) values (
        camera_row.id, observed,
        deerid.try_numeric(c#>>'{status,batteryLevel}'), deerid.try_numeric(c#>>'{status,signalLevel}'),
        deerid.try_numeric(c#>>'{status,temperature}'), deerid.try_numeric(c#>>'{status,memory}'),
        deerid.try_numeric(c#>>'{status,memoryLimit}'), deerid.try_numeric(c#>>'{status,internalVoltage}'),
        deerid.try_numeric(c#>>'{status,externalVoltage}'), c#>>'{status,voltageSource}',
        deerid.try_numeric(c#>>'{status,solarBatteryPercent}'), coalesce(c#>>'{status,sdCard}', c#>>'{status,sdCardStatus}'),
        deerid.try_timestamptz(c#>>'{status,lastTransmissionTime}'), c#>>'{status,servingCell}', c->'status'
      )
      on conflict (camera_id, observed_at) do update set raw_payload = excluded.raw_payload;
    end if;

    if jsonb_typeof(c->'settings') = 'array' then
      insert into deerid.camera_settings_snapshots (camera_id, observed_at, settings)
      values (camera_row.id, p_observed_at, c->'settings')
      on conflict (camera_id, observed_at) do update set settings = excluded.settings;
    end if;
  end loop;

  for item in select value from jsonb_array_elements(coalesce(p_media, '[]'::jsonb)) loop
    photo := item->'provider';
    if jsonb_typeof(photo) <> 'object'
       or nullif(photo->>'photoId', '') is null
       or nullif(photo->>'cameraId', '') is null
       or deerid.try_timestamptz(photo->>'photoDateUtc') is null
       or nullif(item->>'object_path', '') is null
       or coalesce(item->>'image_sha256', '') !~ '^[0-9a-f]{64}$' then
      raise exception 'invalid verified media catalog item';
    end if;

    insert into deerid.media (
      provider_photo_id, camera_id, provider_camera_id, captured_at, synchronized_at,
      media_type, ownership_type, variant, hd_photo, has_headshot, delay_syncing,
      battery_level, signal_level, object_path, image_sha256, image_bytes,
      width, height, content_type, filename, last_seen_at, raw_payload
    ) values (
      photo->>'photoId',
      (select id from deerid.cameras where provider = 'reveal' and provider_camera_id = photo->>'cameraId'),
      photo->>'cameraId', deerid.try_timestamptz(photo->>'photoDateUtc'),
      coalesce(deerid.try_timestamptz(photo->>'lastSynchronizedAt'), deerid.try_timestamptz(photo->>'lastSyncTime')),
      photo->>'type', photo->>'ownershipType',
      case when coalesce(deerid.try_numeric(photo->>'hdPhoto'), 0) <> 0 or lower(coalesce(photo->>'hdPhoto','')) = 'true'
        then 'cloud_hd' else 'cloud_thumbnail' end,
      case when jsonb_typeof(photo->'hdPhoto') = 'boolean' then (photo->>'hdPhoto')::boolean else null end,
      case when jsonb_typeof(photo->'hasHeadshot') = 'boolean' then (photo->>'hasHeadshot')::boolean else null end,
      case when jsonb_typeof(photo->'delaySyncing') = 'boolean' then (photo->>'delaySyncing')::boolean else null end,
      deerid.try_numeric(photo->>'batteryLevel'), deerid.try_numeric(photo->>'signalLevel'),
      item->>'object_path', item->>'image_sha256', (item->>'image_bytes')::bigint,
      nullif(item->>'width','')::integer, nullif(item->>'height','')::integer,
      item->>'content_type', photo->>'filename', p_observed_at, photo
    )
    on conflict (provider, provider_photo_id) do update set
      camera_id = excluded.camera_id,
      synchronized_at = coalesce(excluded.synchronized_at, deerid.media.synchronized_at),
      variant = excluded.variant,
      hd_photo = excluded.hd_photo,
      has_headshot = excluded.has_headshot,
      delay_syncing = excluded.delay_syncing,
      battery_level = excluded.battery_level,
      signal_level = excluded.signal_level,
      object_path = excluded.object_path,
      image_sha256 = excluded.image_sha256,
      image_bytes = excluded.image_bytes,
      width = excluded.width,
      height = excluded.height,
      content_type = excluded.content_type,
      filename = excluded.filename,
      last_seen_at = excluded.last_seen_at,
      raw_payload = excluded.raw_payload
    returning * into media_row;
    media_total := media_total + 1;

    lat := deerid.try_numeric(coalesce(
      photo#>>'{gpsLocation,lat}', photo#>>'{gpsLocation,latitude}',
      photo#>>'{gps,lat}', photo#>>'{gps,latitude}',
      photo#>>'{location,lat}', photo#>>'{location,latitude}',
      photo->>'latitude'
    ));
    lon := deerid.try_numeric(coalesce(
      photo#>>'{gpsLocation,lon}', photo#>>'{gpsLocation,longitude}',
      photo#>>'{gps,lon}', photo#>>'{gps,longitude}',
      photo#>>'{location,lon}', photo#>>'{location,longitude}',
      photo->>'longitude'
    ));
    if media_row.camera_id is not null and lat between -90 and 90 and lon between -180 and 180 then
      insert into deerid.camera_locations (camera_id, latitude, longitude, observed_at, source, raw_payload)
      values (
        media_row.camera_id, lat, lon, media_row.captured_at, 'provider_photo',
        coalesce(photo->'gpsLocation', photo->'gps', photo->'location', '{}'::jsonb)
      )
      on conflict (camera_id, observed_at, source) do update set
        latitude = excluded.latitude, longitude = excluded.longitude, raw_payload = excluded.raw_payload;
    end if;

    weather := coalesce(photo->'weather', photo->'weatherData');
    if jsonb_typeof(weather) = 'object' then
      insert into deerid.media_weather (
        media_id, observed_at, condition, temperature, pressure, pressure_tendency,
        minimum_temperature_12h, maximum_temperature_12h, temperature_departure_24h,
        wind_direction_degrees, wind_direction_short, wind_direction_long,
        wind_speed, wind_gust, moon_phase, sun_phase, raw_payload
      ) values (
        media_row.id, coalesce(deerid.try_timestamptz(weather->>'observationTime'), media_row.captured_at),
        coalesce(weather->>'condition', weather->>'weatherCondition'), deerid.try_numeric(weather->>'temperature'),
        deerid.try_numeric(weather->>'pressure'), weather->>'pressureTendency',
        deerid.try_numeric(coalesce(weather->>'minimumTemperature12Hour', weather->>'minTemperature12Hour')),
        deerid.try_numeric(coalesce(weather->>'maximumTemperature12Hour', weather->>'maxTemperature12Hour')),
        deerid.try_numeric(weather->>'temperatureDeparture24Hour'),
        deerid.try_numeric(weather->>'windDirectionDegrees'), weather->>'windDirectionShort',
        weather->>'windDirectionLong', deerid.try_numeric(weather->>'windSpeed'),
        deerid.try_numeric(weather->>'windGust'), weather->>'moonPhase', weather->>'sunPhase', weather
      )
      on conflict (media_id) do update set
        observed_at = excluded.observed_at, condition = excluded.condition,
        temperature = excluded.temperature, pressure = excluded.pressure,
        raw_payload = excluded.raw_payload;
    end if;

    insert into deerid.classification_jobs (media_id, stage, model_name, model_version, status, input_variant)
    values (media_row.id, 'triage', 'unassigned', null, 'pending', media_row.variant)
    on conflict (media_id, stage, model_name, model_version) do nothing;
  end loop;

  insert into deerid.ingestion_runs (
    started_at, finished_at, status, camera_count, media_count, details
  ) values (
    p_observed_at, now(), 'succeeded', camera_total, media_total,
    jsonb_build_object('catalog_version', 1)
  );

  return jsonb_build_object('ok', true, 'cameras', camera_total, 'media', media_total);
end;
$$;

revoke all on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) from public;
revoke all on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) from anon;
revoke all on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) from authenticated;
grant execute on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) to service_role;

create or replace function public.deerid_private_library(p_limit integer default 60)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select coalesce(jsonb_agg(to_jsonb(feed) order by feed.captured_at desc), '[]'::jsonb)
  from (
    select
      m.id,
      m.captured_at,
      m.camera_id,
      c.name as camera_name,
      m.variant,
      m.width,
      m.height,
      m.hd_photo,
      m.has_headshot,
      m.battery_level,
      m.signal_level,
      coalesce((
        select jsonb_agg(jsonb_build_object(
          'namespace', l.namespace,
          'label', l.label,
          'source', l.source,
          'confidence', l.confidence,
          'status', l.status
        ) order by l.created_at)
        from deerid.media_labels l
        where l.media_id = m.id
      ), '[]'::jsonb) as labels,
      coalesce((
        select jsonb_agg(jsonb_build_object(
          'animal_id', a.id,
          'profile_id', ap.id,
          'display_name', a.display_name,
          'season_year', ap.season_year,
          'confirmation_status', am.confirmation_status,
          'match_confidence', am.match_confidence
        ) order by a.display_name)
        from deerid.animal_media am
        join deerid.animal_profiles ap on ap.id = am.animal_profile_id
        join deerid.animals a on a.id = ap.animal_id
        where am.media_id = m.id
      ), '[]'::jsonb) as animals
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    order by m.captured_at desc
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  ) feed;
$$;

create or replace function public.deerid_private_camera_map()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select coalesce(jsonb_agg(to_jsonb(mapped) order by mapped.name), '[]'::jsonb)
  from (
    select
      c.id,
      c.name,
      c.location_name,
      c.hardware_version,
      c.last_seen_at,
      loc.latitude,
      loc.longitude,
      loc.observed_at,
      status.battery_level,
      status.signal_level
    from deerid.cameras c
    left join lateral (
      select l.latitude, l.longitude, l.observed_at
      from deerid.camera_locations l
      where l.camera_id = c.id
      order by l.observed_at desc
      limit 1
    ) loc on true
    left join lateral (
      select s.battery_level, s.signal_level
      from deerid.camera_status_observations s
      where s.camera_id = c.id
      order by s.observed_at desc
      limit 1
    ) status on true
  ) mapped;
$$;

create or replace function public.deerid_private_media_object(p_media_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select jsonb_build_object('object_path', m.object_path, 'content_type', m.content_type)
  from deerid.media m
  where m.id = p_media_id;
$$;

revoke all on function public.deerid_private_library(integer) from public, anon, authenticated;
revoke all on function public.deerid_private_camera_map() from public, anon, authenticated;
revoke all on function public.deerid_private_media_object(uuid) from public, anon, authenticated;
grant execute on function public.deerid_private_library(integer) to service_role;
grant execute on function public.deerid_private_camera_map() to service_role;
grant execute on function public.deerid_private_media_object(uuid) to service_role;
