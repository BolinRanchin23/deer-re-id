-- Make profile assignment fresh-item safe, replay-idempotent, and append-only auditable.
-- deerid.animal_media remains the current materialized relationship; this ledger is canonical history.

create table deerid.profile_assignment_events (
  id bigint generated always as identity primary key,
  animal_profile_id uuid not null references deerid.animal_profiles(id) on delete restrict,
  media_id uuid not null references deerid.media(id) on delete restrict,
  gate1_assessment_id bigint not null references deerid.gate1_assessments(id) on delete restrict,
  review_version integer not null check (review_version >= 0),
  action text not null check (action in ('create', 'confirm')),
  actor_kind text not null default 'prototype_human' check (actor_kind in ('prototype_human')),
  actor_id uuid references auth.users(id) on delete set null,
  prior_snapshot jsonb,
  resulting_snapshot jsonb not null,
  recorded_at timestamptz not null default now(),
  unique (animal_profile_id, media_id, gate1_assessment_id, review_version, action)
);

alter table deerid.profile_assignment_events enable row level security;
revoke all on table deerid.profile_assignment_events from public, anon, authenticated, service_role;
revoke all on sequence deerid.profile_assignment_events_id_seq from public, anon, authenticated, service_role;

create or replace function deerid.profile_assignment_events_are_append_only()
returns trigger
language plpgsql
set search_path = pg_catalog, public, deerid, pg_temp
as $$
begin
  raise exception 'profile assignment events are append-only';
end;
$$;

create trigger profile_assignment_events_are_append_only
before update or delete on deerid.profile_assignment_events
for each row execute function deerid.profile_assignment_events_are_append_only();

create or replace function public.deerid_create_profile_from_review(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_display_name text,
  p_species text,
  p_sex text,
  p_notes text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  media_captured_at timestamptz;
  latest_assessment_id bigint;
  review_state deerid.gate1_review_state%rowtype;
  created_animal_id uuid;
  created_profile_id uuid;
  existing_profile_id uuid;
  existing_animal_id uuid;
  request_key text;
  result_snapshot jsonb;
begin
  if p_display_name is null or length(trim(p_display_name)) not between 1 and 80
     or p_species not in ('white-tailed deer', 'axis deer', 'other deer')
     or p_sex not in ('male', 'female', 'unknown')
     or p_notes is not null and length(p_notes) > 2000 then
    raise exception 'invalid deer profile';
  end if;

  select a.id, m.captured_at
    into latest_assessment_id, media_captured_at
  from deerid.media m
  join deerid.gate1_assessments a on a.media_id = m.id
  where m.id = p_media_id
    and a.route = 'review'
    and a.is_representative
  order by a.created_at desc, a.id desc
  limit 1;

  if latest_assessment_id is null or latest_assessment_id <> p_assessment_id then
    raise exception 'stale review capability';
  end if;

  insert into deerid.gate1_review_state (gate1_assessment_id)
  values (p_assessment_id)
  on conflict (gate1_assessment_id) do nothing;

  select * into review_state
  from deerid.gate1_review_state
  where gate1_assessment_id = p_assessment_id
  for update;

  if review_state.version <> p_review_version
     or review_state.pending_hd
     or review_state.resolved then
    raise exception 'stale review capability';
  end if;

  request_key := p_media_id::text || E'\x1f' || lower(trim(p_display_name)) || E'\x1f'
    || p_species || E'\x1f' || p_sex || E'\x1f'
    || extract(year from media_captured_at)::integer::text;
  perform pg_advisory_xact_lock(hashtextextended(request_key, 0));

  select ap.id, ap.animal_id
    into existing_profile_id, existing_animal_id
  from deerid.animal_profiles ap
  join deerid.animals a on a.id = ap.animal_id
  join deerid.animal_media am on am.animal_profile_id = ap.id
  where am.media_id = p_media_id
    and ap.season_year = extract(year from media_captured_at)::integer
    and lower(trim(a.display_name)) = lower(trim(p_display_name))
    and a.species = p_species
    and coalesce(a.sex, 'unknown') = p_sex
  order by ap.created_at, ap.id
  limit 1;

  if existing_profile_id is not null then
    return jsonb_build_object(
      'ok', true,
      'created', false,
      'animal_id', existing_animal_id,
      'profile_id', existing_profile_id,
      'season_year', extract(year from media_captured_at)::integer
    );
  end if;

  insert into deerid.animals (species, display_name, sex, notes)
  values (p_species, trim(p_display_name), nullif(p_sex, 'unknown'), p_notes)
  returning id into created_animal_id;

  insert into deerid.animal_profiles (animal_id, season_year)
  values (created_animal_id, extract(year from media_captured_at)::integer)
  returning id into created_profile_id;

  insert into deerid.animal_media (
    animal_profile_id, media_id, match_source, match_confidence,
    confirmation_status, confirmed_by
  ) values (
    created_profile_id, p_media_id, 'human', 1,
    'confirmed', auth.uid()
  );

  result_snapshot := jsonb_build_object(
    'match_source', 'human',
    'match_confidence', 1,
    'confirmation_status', 'confirmed',
    'confirmed_by', auth.uid()
  );

  insert into deerid.profile_assignment_events (
    animal_profile_id, media_id, gate1_assessment_id, review_version,
    action, actor_id, prior_snapshot, resulting_snapshot
  ) values (
    created_profile_id, p_media_id, p_assessment_id, p_review_version,
    'create', auth.uid(), null, result_snapshot
  );

  return jsonb_build_object(
    'ok', true,
    'created', true,
    'animal_id', created_animal_id,
    'profile_id', created_profile_id,
    'season_year', extract(year from media_captured_at)::integer
  );
end;
$$;

create or replace function public.deerid_attach_media_to_profile_from_review(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_profile_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  media_captured_at timestamptz;
  latest_assessment_id bigint;
  profile_year integer;
  profile_active boolean;
  review_state deerid.gate1_review_state%rowtype;
  existing_assignment deerid.animal_media%rowtype;
  prior jsonb;
  result_snapshot jsonb;
begin
  select a.id, m.captured_at
    into latest_assessment_id, media_captured_at
  from deerid.media m
  join deerid.gate1_assessments a on a.media_id = m.id
  where m.id = p_media_id
    and a.route = 'review'
    and a.is_representative
  order by a.created_at desc, a.id desc
  limit 1;

  select ap.season_year, (ap.active and a.status = 'active')
    into profile_year, profile_active
  from deerid.animal_profiles ap
  join deerid.animals a on a.id = ap.animal_id
  where ap.id = p_profile_id;

  if latest_assessment_id is null
     or latest_assessment_id <> p_assessment_id
     or not coalesce(profile_active, false)
     or profile_year is null
     or profile_year <> extract(year from media_captured_at)::integer then
    raise exception 'invalid profile assignment';
  end if;

  insert into deerid.gate1_review_state (gate1_assessment_id)
  values (p_assessment_id)
  on conflict (gate1_assessment_id) do nothing;

  select * into review_state
  from deerid.gate1_review_state
  where gate1_assessment_id = p_assessment_id
  for update;

  if review_state.version <> p_review_version
     or review_state.pending_hd
     or review_state.resolved then
    raise exception 'stale review capability';
  end if;

  select * into existing_assignment
  from deerid.animal_media
  where animal_profile_id = p_profile_id
    and media_id = p_media_id
  for update;
  if found then
    prior := to_jsonb(existing_assignment);
  else
    prior := null;
  end if;

  result_snapshot := jsonb_build_object(
    'match_source', 'human',
    'match_confidence', 1,
    'confirmation_status', 'confirmed',
    'confirmed_by', auth.uid()
  );

  insert into deerid.profile_assignment_events (
    animal_profile_id, media_id, gate1_assessment_id, review_version,
    action, actor_id, prior_snapshot, resulting_snapshot
  ) values (
    p_profile_id, p_media_id, p_assessment_id, p_review_version,
    'confirm', auth.uid(), prior, result_snapshot
  ) on conflict (animal_profile_id, media_id, gate1_assessment_id, review_version, action)
    do nothing;

  insert into deerid.animal_media (
    animal_profile_id, media_id, match_source, match_confidence,
    confirmation_status, confirmed_by
  ) values (
    p_profile_id, p_media_id, 'human', 1,
    'confirmed', auth.uid()
  )
  on conflict (animal_profile_id, media_id) do update set
    match_source = excluded.match_source,
    match_confidence = excluded.match_confidence,
    confirmation_status = excluded.confirmation_status,
    confirmed_by = excluded.confirmed_by;

  return jsonb_build_object(
    'ok', true,
    'profile_id', p_profile_id,
    'season_year', profile_year
  );
end;
$$;

revoke all on function public.deerid_create_profile_from_review(uuid, bigint, integer, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.deerid_create_profile_from_review(uuid, bigint, integer, text, text, text, text)
  to service_role;

revoke all on function public.deerid_attach_media_to_profile_from_review(uuid, bigint, integer, uuid)
  from public, anon, authenticated;
grant execute on function public.deerid_attach_media_to_profile_from_review(uuid, bigint, integer, uuid)
  to service_role;
