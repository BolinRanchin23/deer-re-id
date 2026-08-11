-- Harden Gate 1 event claims, review capabilities, and unresolved queue priority.

create unique index gate1_assessment_model_once_idx
  on deerid.gate1_assessments(media_id, model_name, model_version);

alter table deerid.review_decisions
  add column review_version integer not null default 0;

create table deerid.gate1_review_state (
  gate1_assessment_id bigint primary key references deerid.gate1_assessments(id) on delete cascade,
  version integer not null default 0 check (version >= 0),
  resolved boolean not null default false,
  updated_at timestamptz not null default now()
);

alter table deerid.gate1_review_state enable row level security;

create unique index review_decision_capability_once_idx
  on deerid.review_decisions(gate1_assessment_id, review_version);

create or replace function public.deerid_gate1_pending(
  p_model_name text,
  p_model_version text,
  p_limit integer default 40
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with ordered as (
    select m.id, m.camera_id, m.captured_at, m.object_path,
      lag(m.captured_at) over (partition by m.camera_id order by m.captured_at, m.id) as previous_at
    from deerid.media m
  ), marked as (
    select *, case when previous_at is null or captured_at - previous_at > interval '5 seconds' then 1 else 0 end as starts_event
    from ordered
  ), numbered as (
    select *, sum(starts_event) over (partition by camera_id order by captured_at, id) as event_number
    from marked
  ), evented as (
    select *, min(captured_at) over (partition by camera_id, event_number) as event_start
    from numbered
  ), keyed as (
    select *, substr(md5(camera_id::text || ':' || event_start::text), 1, 24) as event_key
    from evented
  ), candidate_events as (
    select event_key, min(event_start) as event_start
    from keyed k
    where not exists (
      select 1 from deerid.gate1_assessments g
      where g.media_id = k.id and g.model_name = p_model_name and g.model_version = p_model_version
    )
    group by event_key
    order by min(event_start)
    limit greatest(1, least(coalesce(p_limit, 40), 50))
  ), pending as (
    select k.id as media_id, k.camera_id, k.captured_at, k.event_key, k.object_path
    from keyed k
    join candidate_events ce on ce.event_key = k.event_key
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'media_id', media_id, 'camera_id', camera_id, 'captured_at', captured_at,
    'event_key', event_key, 'object_path', object_path
  ) order by captured_at, media_id), '[]'::jsonb)
  from pending;
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
  affected integer := 0;
begin
  if jsonb_typeof(p_results) <> 'array' or jsonb_array_length(p_results) > 500
     or length(p_model_name) not between 1 and 120 or length(p_model_version) not between 1 and 120 then
    raise exception 'invalid gate1 batch';
  end if;
  for item in select value from jsonb_array_elements(p_results) loop
    if coalesce(item->>'route', '') not in ('review', 'archive', 'event_duplicate')
       or length(coalesce(item->>'event_key', '')) not between 8 and 80 then
      raise exception 'invalid gate1 result';
    end if;
    insert into deerid.gate1_assessments (
      media_id, event_key, route, reason, is_representative, model_name, model_version,
      animal_confidence, animal_area, species_label, species_confidence, detections, raw_output
    ) values (
      (item->>'media_id')::uuid, item->>'event_key', item->>'route', coalesce(item->>'reason', 'unspecified'),
      coalesce((item->>'is_representative')::boolean, false), p_model_name, p_model_version,
      coalesce((item->>'animal_confidence')::double precision, 0),
      coalesce((item->>'animal_area')::double precision, 0), nullif(item->>'species_label', ''),
      coalesce((item->>'species_confidence')::double precision, 0),
      coalesce(item->'detections', '[]'::jsonb), coalesce(item->'raw_output', '{}'::jsonb)
    ) on conflict (media_id, model_name, model_version) do nothing;
    get diagnostics affected = row_count;
    inserted_count := inserted_count + affected;
  end loop;
  return jsonb_build_object('ok', true, 'inserted', inserted_count);
end;
$$;

drop function public.deerid_record_review_decision(uuid, text, text);

create or replace function public.deerid_record_review_decision(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_action text,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment_id bigint;
  decision_id bigint;
  advanced_id bigint;
begin
  if p_action not in ('request_hd', 'keep_for_identity', 'not_useful', 'defer')
     or length(coalesce(p_note, '')) > 500 or p_review_version < 0 then
    raise exception 'invalid review decision';
  end if;
  select id into latest_assessment_id from deerid.gate1_assessments
  where media_id = p_media_id and route = 'review' and is_representative
  order by created_at desc, id desc limit 1;
  if latest_assessment_id is distinct from p_assessment_id then
    raise exception 'stale review capability';
  end if;
  insert into deerid.gate1_review_state (gate1_assessment_id)
  values (p_assessment_id) on conflict do nothing;
  update deerid.gate1_review_state
  set version = version + 1, resolved = (p_action <> 'defer'), updated_at = now()
  where gate1_assessment_id = p_assessment_id and version = p_review_version and not resolved
  returning gate1_assessment_id into advanced_id;
  if advanced_id is null then raise exception 'stale or resolved review capability'; end if;
  insert into deerid.review_decisions (media_id, gate1_assessment_id, review_version, action, note)
  values (p_media_id, p_assessment_id, p_review_version, p_action, nullif(trim(coalesce(p_note, '')), ''))
  returning id into decision_id;
  if p_action = 'request_hd' then
    insert into deerid.hd_requests (media_id, requested_by_decision_id)
    values (p_media_id, decision_id) on conflict (media_id) do nothing;
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
  select coalesce(jsonb_agg(to_jsonb(feed) - 'queue_priority' order by feed.captured_at desc), '[]'::jsonb)
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
        'species_confidence', g.species_confidence, 'review_version', coalesce(s.version, 0),
        'created_at', g.created_at) end as gate1,
      case when r.id is null then null else jsonb_build_object(
        'action', r.action, 'note', r.note, 'decided_at', r.decided_at) end as review_decision,
      case when g.route = 'review' and g.is_representative and not coalesce(s.resolved, false) then 0 else 1 end as queue_priority
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    left join lateral (
      select * from deerid.gate1_assessments ga where ga.media_id = m.id
      order by ga.created_at desc, ga.id desc limit 1
    ) g on true
    left join deerid.gate1_review_state s on s.gate1_assessment_id = g.id
    left join lateral (
      select * from deerid.review_decisions rd where rd.gate1_assessment_id = g.id
      order by rd.decided_at desc, rd.id desc limit 1
    ) r on true
    order by queue_priority, m.captured_at desc
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  ) feed;
$$;

revoke all on function public.deerid_record_review_decision(uuid, bigint, integer, text, text) from public, anon, authenticated;
grant execute on function public.deerid_record_review_decision(uuid, bigint, integer, text, text) to service_role;
