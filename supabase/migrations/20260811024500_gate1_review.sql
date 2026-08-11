-- Gate 1: versioned model evidence, human review actions, and HD request queue.

create table deerid.gate1_assessments (
  id bigint generated always as identity primary key,
  media_id uuid not null references deerid.media(id) on delete cascade,
  event_key text not null check (length(event_key) between 8 and 80),
  route text not null check (route in ('review', 'archive', 'event_duplicate')),
  reason text not null check (length(reason) between 1 and 120),
  is_representative boolean not null,
  model_name text not null check (length(model_name) between 1 and 120),
  model_version text not null check (length(model_version) between 1 and 120),
  animal_confidence double precision not null check (animal_confidence between 0 and 1),
  animal_area double precision not null check (animal_area between 0 and 1),
  species_label text,
  species_confidence double precision not null check (species_confidence between 0 and 1),
  detections jsonb not null default '[]'::jsonb,
  raw_output jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index gate1_assessments_media_latest_idx
  on deerid.gate1_assessments (media_id, created_at desc, id desc);
create index gate1_assessments_review_idx
  on deerid.gate1_assessments (route, is_representative, created_at desc);

create table deerid.review_decisions (
  id bigint generated always as identity primary key,
  media_id uuid not null references deerid.media(id) on delete cascade,
  gate1_assessment_id bigint references deerid.gate1_assessments(id) on delete set null,
  action text not null check (action in ('request_hd', 'keep_for_identity', 'not_useful', 'defer')),
  note text check (note is null or length(note) <= 500),
  decided_at timestamptz not null default now()
);

create index review_decisions_media_latest_idx
  on deerid.review_decisions (media_id, decided_at desc, id desc);

create table deerid.hd_requests (
  id bigint generated always as identity primary key,
  media_id uuid not null unique references deerid.media(id) on delete cascade,
  status text not null default 'queued' check (status in ('queued', 'requesting', 'available', 'failed', 'cancelled')),
  requested_by_decision_id bigint references deerid.review_decisions(id) on delete set null,
  provider_request_id text,
  attempts integer not null default 0 check (attempts >= 0),
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table deerid.gate1_assessments enable row level security;
alter table deerid.review_decisions enable row level security;
alter table deerid.hd_requests enable row level security;

create or replace function public.deerid_gate1_pending(
  p_model_name text,
  p_model_version text,
  p_limit integer default 60
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select coalesce(jsonb_agg(to_jsonb(candidate) order by candidate.captured_at), '[]'::jsonb)
  from (
    select m.id as media_id, m.camera_id, m.captured_at, m.object_path, m.content_type
    from deerid.media m
    where m.variant = 'cloud_thumbnail'
      and not exists (
        select 1 from deerid.gate1_assessments a
        where a.media_id = m.id
          and a.model_name = p_model_name
          and a.model_version = p_model_version
      )
    order by m.captured_at
    limit greatest(1, least(coalesce(p_limit, 60), 100))
  ) candidate;
$$;

create or replace function public.deerid_record_gate1_batch(
  p_model_name text,
  p_model_version text,
  p_results jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  item jsonb;
  inserted_count integer := 0;
begin
  if p_model_name is null or length(p_model_name) not between 1 and 120
     or p_model_version is null or length(p_model_version) not between 1 and 120
     or jsonb_typeof(p_results) <> 'array'
     or jsonb_array_length(p_results) > 100 then
    raise exception 'invalid gate1 batch';
  end if;
  for item in select value from jsonb_array_elements(p_results)
  loop
    insert into deerid.gate1_assessments (
      media_id, event_key, route, reason, is_representative,
      model_name, model_version, animal_confidence, animal_area,
      species_label, species_confidence, detections, raw_output
    ) values (
      (item->>'media_id')::uuid,
      item->>'event_key', item->>'route', item->>'reason',
      coalesce((item->>'is_representative')::boolean, false),
      p_model_name, p_model_version,
      coalesce((item->>'animal_confidence')::double precision, 0),
      coalesce((item->>'animal_area')::double precision, 0),
      nullif(item->>'species_label', ''),
      coalesce((item->>'species_confidence')::double precision, 0),
      coalesce(item->'detections', '[]'::jsonb),
      coalesce(item->'raw_output', '{}'::jsonb)
    );
    inserted_count := inserted_count + 1;
  end loop;
  return jsonb_build_object('ok', true, 'inserted', inserted_count);
end;
$$;

create or replace function public.deerid_record_review_decision(
  p_media_id uuid,
  p_action text,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment deerid.gate1_assessments%rowtype;
  decision_id bigint;
begin
  if p_action not in ('request_hd', 'keep_for_identity', 'not_useful', 'defer')
     or length(coalesce(p_note, '')) > 500 then
    raise exception 'invalid review decision';
  end if;
  select * into latest_assessment
  from deerid.gate1_assessments
  where media_id = p_media_id and route = 'review' and is_representative
  order by created_at desc, id desc limit 1;
  if latest_assessment.id is null then
    raise exception 'media is not in review queue';
  end if;
  insert into deerid.review_decisions (media_id, gate1_assessment_id, action, note)
  values (p_media_id, latest_assessment.id, p_action, nullif(trim(coalesce(p_note, '')), ''))
  returning id into decision_id;
  if p_action = 'request_hd' then
    insert into deerid.hd_requests (media_id, requested_by_decision_id)
    values (p_media_id, decision_id)
    on conflict (media_id) do nothing;
  end if;
  return jsonb_build_object('ok', true, 'media_id', p_media_id, 'action', p_action, 'decision_id', decision_id);
end;
$$;

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
      m.id, m.captured_at, m.camera_id, c.name as camera_name, m.variant,
      m.width, m.height, m.hd_photo, m.has_headshot, m.battery_level, m.signal_level,
      coalesce((select jsonb_agg(jsonb_build_object(
        'namespace', l.namespace, 'label', l.label, 'source', l.source,
        'confidence', l.confidence, 'status', l.status) order by l.created_at)
        from deerid.media_labels l where l.media_id = m.id), '[]'::jsonb) as labels,
      coalesce((select jsonb_agg(jsonb_build_object(
        'animal_id', a.id, 'profile_id', ap.id, 'display_name', a.display_name,
        'season_year', ap.season_year, 'confirmation_status', am.confirmation_status,
        'match_confidence', am.match_confidence) order by a.display_name)
        from deerid.animal_media am
        join deerid.animal_profiles ap on ap.id = am.animal_profile_id
        join deerid.animals a on a.id = ap.animal_id
        where am.media_id = m.id), '[]'::jsonb) as animals,
      case when g.id is null then null else jsonb_build_object(
        'id', g.id, 'event_key', g.event_key, 'route', g.route, 'reason', g.reason,
        'is_representative', g.is_representative, 'model_name', g.model_name,
        'model_version', g.model_version, 'animal_confidence', g.animal_confidence,
        'animal_area', g.animal_area, 'species_label', g.species_label,
        'species_confidence', g.species_confidence, 'created_at', g.created_at) end as gate1,
      case when r.id is null then null else jsonb_build_object(
        'action', r.action, 'note', r.note, 'decided_at', r.decided_at) end as review_decision
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    left join lateral (
      select * from deerid.gate1_assessments ga where ga.media_id = m.id
      order by ga.created_at desc, ga.id desc limit 1
    ) g on true
    left join lateral (
      select * from deerid.review_decisions rd where rd.media_id = m.id
      order by rd.decided_at desc, rd.id desc limit 1
    ) r on true
    order by m.captured_at desc
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  ) feed;
$$;

revoke all on function public.deerid_gate1_pending(text, text, integer) from public, anon, authenticated;
revoke all on function public.deerid_record_gate1_batch(text, text, jsonb) from public, anon, authenticated;
revoke all on function public.deerid_record_review_decision(uuid, text, text) from public, anon, authenticated;
grant execute on function public.deerid_gate1_pending(text, text, integer) to service_role;
grant execute on function public.deerid_record_gate1_batch(text, text, jsonb) to service_role;
grant execute on function public.deerid_record_review_decision(uuid, text, text) to service_role;
