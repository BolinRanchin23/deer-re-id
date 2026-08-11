-- Gate 1B: append-only male/antler/species evidence, human corrections,
-- conservative review queues, validation metrics, and HD priority.

create table deerid.gate1b_predictions (
  id bigint generated always as identity primary key,
  media_id uuid not null references deerid.media(id) on delete restrict,
  gate1_assessment_id bigint not null references deerid.gate1_assessments(id) on delete restrict,
  event_key text not null check (length(event_key) between 8 and 80),
  model_name text not null check (length(model_name) between 1 and 120),
  model_version text not null check (length(model_version) between 1 and 160),
  species_label text not null check (species_label in ('whitetail', 'axis', 'other_deer', 'non_deer', 'unknown')),
  visible_antler text not null check (visible_antler in ('yes', 'no', 'unknown')),
  probable_male text not null check (probable_male in ('yes', 'no', 'unknown')),
  head_visibility text not null check (head_visibility in ('full', 'partial', 'none', 'unknown')),
  lighting text not null check (lighting in ('day_color', 'night_ir', 'unknown')),
  animal_count integer not null check (animal_count between 0 and 20),
  mixed_group boolean not null,
  all_animals_assessed boolean not null,
  triage_class text not null check (triage_class in ('likely_male', 'uncertain', 'female_candidate')),
  hd_recommended boolean not null default false,
  model_failure boolean not null default false,
  reason text not null check (length(reason) between 1 and 300),
  raw_output jsonb not null,
  created_at timestamptz not null default now(),
  unique (gate1_assessment_id, model_name, model_version)
);

create index gate1b_predictions_media_latest_idx
  on deerid.gate1b_predictions (media_id, created_at desc, id desc);
create index gate1b_predictions_triage_idx
  on deerid.gate1b_predictions (triage_class, created_at desc);

create table deerid.gate1b_human_labels (
  id bigint generated always as identity primary key,
  media_id uuid not null references deerid.media(id) on delete restrict,
  gate1_assessment_id bigint not null references deerid.gate1_assessments(id) on delete restrict,
  supersedes_id bigint references deerid.gate1b_human_labels(id) on delete restrict,
  species_label text not null check (species_label in ('whitetail', 'axis', 'other_deer', 'non_deer', 'unknown')),
  visible_antler text not null check (visible_antler in ('yes', 'no', 'unknown')),
  probable_male text not null check (probable_male in ('yes', 'no', 'unknown')),
  head_visibility text not null check (head_visibility in ('full', 'partial', 'none', 'unknown')),
  note text check (note is null or length(note) <= 500),
  source text not null default 'human_review' check (source = 'human_review'),
  created_at timestamptz not null default now()
);

create index gate1b_human_labels_assessment_latest_idx
  on deerid.gate1b_human_labels (gate1_assessment_id, created_at desc, id desc);

create table deerid.gate1b_policy (
  singleton boolean primary key default true check (singleton),
  policy_version text not null,
  suppression_enabled boolean not null default false,
  female_audit_percent integer not null default 10 check (female_audit_percent between 1 and 100),
  minimum_labels integer not null default 100 check (minimum_labels >= 20),
  minimum_buck_events integer not null default 20 check (minimum_buck_events >= 5),
  required_buck_recall double precision not null default 0.99 check (required_buck_recall between 0 and 1),
  updated_at timestamptz not null default now()
);
insert into deerid.gate1b_policy (policy_version) values ('gate1b-policy-2026-08-11.1');

alter table deerid.gate1b_predictions enable row level security;
alter table deerid.gate1b_human_labels enable row level security;
alter table deerid.gate1b_policy enable row level security;

grant select, insert on deerid.gate1b_predictions, deerid.gate1b_human_labels to service_role;
grant select on deerid.gate1b_policy to service_role;
grant usage, select on sequence deerid.gate1b_predictions_id_seq to service_role;
grant usage, select on sequence deerid.gate1b_human_labels_id_seq to service_role;
revoke update, delete, truncate on deerid.gate1b_predictions, deerid.gate1b_human_labels from service_role;
revoke insert, update, delete, truncate on deerid.gate1b_policy from service_role;

create or replace function deerid.reject_gate1b_mutation()
returns trigger language plpgsql set search_path = pg_catalog, deerid, pg_temp as $$
begin
  raise exception 'Gate 1B evidence is append-only';
end;
$$;
create trigger gate1b_predictions_append_only before update or delete on deerid.gate1b_predictions
for each row execute function deerid.reject_gate1b_mutation();
create trigger gate1b_human_labels_append_only before update or delete on deerid.gate1b_human_labels
for each row execute function deerid.reject_gate1b_mutation();
revoke all on function deerid.reject_gate1b_mutation() from public, anon, authenticated;

create or replace function public.deerid_gate1b_pending(
  p_model_name text,
  p_model_version text,
  p_limit integer default 20
)
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with latest_gate1 as (
    select distinct on (ga.media_id) ga.*
    from deerid.gate1_assessments ga
    order by ga.media_id, ga.created_at desc, ga.id desc
  ), candidates as (
    select m.id as media_id, g.id as gate1_assessment_id, g.event_key,
      m.camera_id, m.captured_at, m.object_path,
      row_number() over (partition by m.camera_id order by
        case when g.route = 'review' then 0 else 1 end, m.captured_at desc) as camera_rank
    from latest_gate1 g
    join deerid.media m on m.id = g.media_id
    where m.variant = 'cloud_thumbnail'
      and g.route <> 'event_duplicate'
      and (g.is_representative or g.route = 'review')
      and not exists (
        select 1 from deerid.gate1b_predictions p
        where p.gate1_assessment_id = g.id
          and p.model_name = p_model_name and p.model_version = p_model_version
      )
  )
  select coalesce(jsonb_agg(to_jsonb(chosen) order by chosen.camera_rank, chosen.camera_id, chosen.captured_at desc), '[]'::jsonb)
  from (
    select media_id, gate1_assessment_id, event_key, camera_id, captured_at, object_path, camera_rank
    from candidates
    order by camera_rank, camera_id, captured_at desc
    limit greatest(1, least(coalesce(p_limit, 20), 60))
  ) chosen;
$$;

create or replace function public.deerid_record_gate1b_batch(
  p_model_name text,
  p_model_version text,
  p_results jsonb
)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare item jsonb; inserted_count integer := 0;
begin
  if p_model_name is null or length(p_model_name) not between 1 and 120
     or p_model_version is null or length(p_model_version) not between 1 and 160
     or jsonb_typeof(p_results) <> 'array' or jsonb_array_length(p_results) > 60 then
    raise exception 'invalid Gate 1B batch';
  end if;
  for item in select value from jsonb_array_elements(p_results) loop
    if (item->>'triage_class') = 'female_candidate' and (
      item->>'species_label' not in ('whitetail', 'axis')
      or item->>'visible_antler' <> 'no' or item->>'probable_male' <> 'no'
      or item->>'head_visibility' <> 'full'
      or coalesce((item->>'all_animals_assessed')::boolean, false) is not true
    ) then raise exception 'unsafe female candidate'; end if;
    insert into deerid.gate1b_predictions (
      media_id, gate1_assessment_id, event_key, model_name, model_version,
      species_label, visible_antler, probable_male, head_visibility, lighting,
      animal_count, mixed_group, all_animals_assessed, triage_class,
      hd_recommended, model_failure, reason, raw_output
    ) values (
      (item->>'media_id')::uuid, (item->>'gate1_assessment_id')::bigint,
      item->>'event_key', p_model_name, p_model_version,
      item->>'species_label', item->>'visible_antler', item->>'probable_male',
      item->>'head_visibility', item->>'lighting', (item->>'animal_count')::integer,
      (item->>'mixed_group')::boolean, (item->>'all_animals_assessed')::boolean,
      item->>'triage_class', coalesce((item->>'hd_recommended')::boolean, false),
      coalesce((item->>'model_failure')::boolean, false), item->>'reason',
      coalesce(item->'raw_output', '{}'::jsonb)
    ) on conflict (gate1_assessment_id, model_name, model_version) do nothing;
    if found then inserted_count := inserted_count + 1; end if;
  end loop;
  return jsonb_build_object('ok', true, 'inserted', inserted_count);
end;
$$;

create or replace function public.deerid_record_gate1b_label(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_species_label text,
  p_visible_antler text,
  p_probable_male text,
  p_head_visibility text,
  p_note text default null
)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare current_assessment bigint; current_version integer; previous_id bigint; new_id bigint;
begin
  if p_species_label not in ('whitetail', 'axis', 'other_deer', 'non_deer', 'unknown')
    or p_visible_antler not in ('yes', 'no', 'unknown')
    or p_probable_male not in ('yes', 'no', 'unknown')
    or p_head_visibility not in ('full', 'partial', 'none', 'unknown')
    or length(coalesce(p_note, '')) > 500 then raise exception 'invalid Gate 1B label'; end if;
  select g.id, coalesce(s.version, 0) into current_assessment, current_version
  from deerid.gate1_assessments g
  left join deerid.gate1_review_state s on s.gate1_assessment_id = g.id
  where g.media_id = p_media_id and g.route = 'review' and g.is_representative
  order by g.created_at desc, g.id desc limit 1;
  if current_assessment is distinct from p_assessment_id or current_version is distinct from p_review_version then
    raise exception 'stale Gate 1B label capability';
  end if;
  select id into previous_id from deerid.gate1b_human_labels
  where gate1_assessment_id = p_assessment_id order by created_at desc, id desc limit 1;
  insert into deerid.gate1b_human_labels (
    media_id, gate1_assessment_id, supersedes_id, species_label,
    visible_antler, probable_male, head_visibility, note
  ) values (
    p_media_id, p_assessment_id, previous_id, p_species_label,
    p_visible_antler, p_probable_male, p_head_visibility,
    nullif(trim(coalesce(p_note, '')), '')
  ) returning id into new_id;
  return jsonb_build_object('ok', true, 'label_id', new_id, 'media_id', p_media_id);
end;
$$;

create or replace function public.deerid_gate1b_metrics()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with latest_prediction as (
    select distinct on (gate1_assessment_id) * from deerid.gate1b_predictions
    order by gate1_assessment_id, created_at desc, id desc
  ), latest_label as (
    select distinct on (gate1_assessment_id) * from deerid.gate1b_human_labels
    order by gate1_assessment_id, created_at desc, id desc
  ), scored as (
    select p.*, l.id as label_id, l.species_label as human_species,
      l.visible_antler as human_antler, l.probable_male as human_male,
      (l.visible_antler = 'yes' or l.probable_male = 'yes') as human_buck,
      (p.triage_class <> 'female_candidate') as safely_routed,
      m.camera_id
    from latest_prediction p
    join deerid.media m on m.id = p.media_id
    left join latest_label l on l.gate1_assessment_id = p.gate1_assessment_id
  ), totals as (
    select count(*)::integer predictions,
      count(*) filter (where triage_class = 'likely_male')::integer likely_male,
      count(*) filter (where triage_class = 'uncertain')::integer uncertain,
      count(*) filter (where triage_class = 'female_candidate')::integer female_candidates,
      count(label_id)::integer human_labels,
      count(*) filter (where human_buck)::integer labeled_buck_events,
      count(*) filter (where human_buck and safely_routed)::integer routed_buck_events,
      count(distinct camera_id) filter (where label_id is not null)::integer labeled_cameras,
      count(*) filter (where label_id is not null and lighting = 'day_color')::integer labeled_day,
      count(*) filter (where label_id is not null and lighting = 'night_ir')::integer labeled_ir,
      count(*) filter (where label_id is not null and human_species = 'axis')::integer labeled_axis
    from scored
  )
  select jsonb_build_object(
    'model_name', 'Ollama-Gemma4-Vision',
    'predictions', t.predictions, 'likely_male', t.likely_male,
    'uncertain', t.uncertain, 'female_candidates', t.female_candidates,
    'human_labels', t.human_labels, 'labeled_buck_events', t.labeled_buck_events,
    'labeled_cameras', t.labeled_cameras, 'labeled_day', t.labeled_day,
    'labeled_ir', t.labeled_ir, 'labeled_axis', t.labeled_axis,
    'buck_recall', case when t.labeled_buck_events = 0 then null
      else t.routed_buck_events::double precision / t.labeled_buck_events end,
    'suppression_enabled', p.suppression_enabled,
    'suppression_ready', (
      t.human_labels >= p.minimum_labels and t.labeled_buck_events >= p.minimum_buck_events
      and t.labeled_cameras >= 4 and t.labeled_day > 0 and t.labeled_ir > 0
      and t.routed_buck_events::double precision / nullif(t.labeled_buck_events, 0) >= p.required_buck_recall
    ),
    'female_audit_percent', p.female_audit_percent,
    'minimum_labels', p.minimum_labels, 'minimum_buck_events', p.minimum_buck_events,
    'required_buck_recall', p.required_buck_recall
  ) from totals t cross join deerid.gate1b_policy p where p.singleton;
$$;

alter table deerid.hd_requests add column priority integer not null default 50 check (priority between 0 and 100);
alter table deerid.hd_requests add column priority_reason text;

create or replace function deerid.assign_gate1b_hd_priority()
returns trigger language plpgsql set search_path = pg_catalog, deerid, pg_temp as $$
declare class text;
begin
  select triage_class into class from deerid.gate1b_predictions
  where media_id = new.media_id order by created_at desc, id desc limit 1;
  if class = 'likely_male' then new.priority := 100; new.priority_reason := 'gate1b_likely_male';
  elsif class = 'female_candidate' then new.priority := 10; new.priority_reason := 'gate1b_female_candidate';
  else new.priority := 50; new.priority_reason := 'gate1b_uncertain_or_pending'; end if;
  return new;
end;
$$;
create trigger hd_requests_gate1b_priority before insert on deerid.hd_requests
for each row execute function deerid.assign_gate1b_hd_priority();
revoke all on function deerid.assign_gate1b_hd_priority() from public, anon, authenticated;

create or replace function public.deerid_claim_queued_hd_request()
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare queued_id bigint; new_token uuid := gen_random_uuid(); provider_id text;
begin
  select h.id, m.provider_photo_id into queued_id, provider_id
  from deerid.hd_requests h join deerid.media m on m.id = h.media_id
  where h.status = 'queued'
  order by h.priority desc, h.created_at, h.id
  for update of h skip locked limit 1;
  if queued_id is null then return jsonb_build_object('ok', true, 'empty', true); end if;
  update deerid.hd_requests set status = 'requesting', request_token = new_token,
    request_started_at = now(), updated_at = now(), last_error = null where id = queued_id;
  return jsonb_build_object('ok', true, 'empty', false, 'request_token', new_token,
    'provider_photo_id', provider_id);
end;
$$;

create or replace function public.deerid_private_library(p_limit integer default 60)
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with policy as (select * from deerid.gate1b_policy where singleton), enriched as (
    select m.*, c.name as camera_name, g.id as gate1_id, g.event_key, g.route, g.reason as gate1_reason,
      g.is_representative, g.model_name as gate1_model_name, g.model_version as gate1_model_version,
      g.animal_confidence, g.animal_area, g.species_label as gate1_species,
      g.species_confidence, g.created_at as gate1_created_at,
      coalesce(s.version, 0) as review_version, coalesce(s.pending_hd, false) as pending_hd,
      coalesce(s.resolved, false) as resolved,
      r.action as review_action, r.note as review_note, r.decided_at,
      p.id as prediction_id, p.model_name as gate1b_model_name, p.model_version as gate1b_model_version,
      p.species_label as predicted_species, p.visible_antler as predicted_antler,
      p.probable_male as predicted_male, p.head_visibility as predicted_head,
      p.lighting, p.animal_count, p.mixed_group, p.all_animals_assessed,
      p.triage_class as predicted_triage, p.hd_recommended, p.model_failure,
      p.reason as gate1b_reason, p.created_at as gate1b_created_at,
      h.id as human_label_id, h.species_label as human_species,
      h.visible_antler as human_antler, h.probable_male as human_male,
      h.head_visibility as human_head, h.created_at as human_labeled_at,
      policy.suppression_enabled, policy.female_audit_percent
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    left join lateral (select * from deerid.gate1_assessments ga where ga.media_id = m.id
      order by ga.created_at desc, ga.id desc limit 1) g on true
    left join deerid.gate1_review_state s on s.gate1_assessment_id = g.id
    left join lateral (select * from deerid.review_decisions rd where rd.gate1_assessment_id = g.id
      order by rd.decided_at desc, rd.id desc limit 1) r on true
    left join lateral (select * from deerid.gate1b_predictions gp where gp.gate1_assessment_id = g.id
      order by gp.created_at desc, gp.id desc limit 1) p on true
    left join lateral (select * from deerid.gate1b_human_labels gh where gh.gate1_assessment_id = g.id
      order by gh.created_at desc, gh.id desc limit 1) h on true
    cross join policy
  ), classified as (
    select e.*, case
      when human_label_id is not null and (human_antler = 'yes' or human_male = 'yes') then 'likely_male'
      when human_label_id is not null and human_species in ('whitetail','axis')
        and human_antler = 'no' and human_male = 'no' and human_head = 'full' then 'female_candidate'
      when human_label_id is not null then 'uncertain'
      else coalesce(predicted_triage, 'uncertain') end as effective_triage
    from enriched e
  ), routed as (
    select x.*, case
      when effective_triage = 'likely_male' then 'likely_male'
      when effective_triage = 'female_candidate'
        and mod(abs(hashtextextended(event_key, 0)), 100) < female_audit_percent then 'female_audit'
      when effective_triage = 'female_candidate' and suppression_enabled then 'suppressed'
      else 'uncertain' end as gate1b_queue
    from classified x
  ), feed as (
    select
      id, captured_at, camera_id, camera_name, variant, width, height, hd_photo, has_headshot,
      battery_level, signal_level,
      coalesce((select jsonb_agg(jsonb_build_object('namespace', l.namespace, 'label', l.label,
        'source', l.source, 'confidence', l.confidence, 'status', l.status) order by l.created_at)
        from deerid.media_labels l where l.media_id = routed.id), '[]'::jsonb) as labels,
      coalesce((select jsonb_agg(jsonb_build_object('animal_id', a.id, 'profile_id', ap.id,
        'display_name', a.display_name, 'season_year', ap.season_year,
        'confirmation_status', am.confirmation_status, 'match_confidence', am.match_confidence)
        order by a.display_name) from deerid.animal_media am
        join deerid.animal_profiles ap on ap.id = am.animal_profile_id
        join deerid.animals a on a.id = ap.animal_id where am.media_id = routed.id), '[]'::jsonb) as animals,
      case when gate1_id is null then null else jsonb_build_object(
        'id', gate1_id, 'event_key', event_key, 'route', route, 'reason', gate1_reason,
        'is_representative', is_representative, 'model_name', gate1_model_name,
        'model_version', gate1_model_version, 'animal_confidence', animal_confidence,
        'animal_area', animal_area, 'species_label', gate1_species,
        'species_confidence', species_confidence, 'review_version', review_version,
        'pending_hd', pending_hd, 'created_at', gate1_created_at) end as gate1,
      case when prediction_id is null and human_label_id is null then null else jsonb_build_object(
        'prediction_id', prediction_id, 'model_name', gate1b_model_name,
        'model_version', gate1b_model_version, 'species_label', predicted_species,
        'visible_antler', predicted_antler, 'probable_male', predicted_male,
        'head_visibility', predicted_head, 'lighting', lighting, 'animal_count', animal_count,
        'mixed_group', mixed_group, 'all_animals_assessed', all_animals_assessed,
        'triage_class', effective_triage, 'queue', gate1b_queue,
        'hd_recommended', coalesce(hd_recommended, false), 'model_failure', coalesce(model_failure, false),
        'reason', gate1b_reason, 'created_at', gate1b_created_at,
        'human_label', case when human_label_id is null then null else jsonb_build_object(
          'id', human_label_id, 'species_label', human_species, 'visible_antler', human_antler,
          'probable_male', human_male, 'head_visibility', human_head,
          'created_at', human_labeled_at) end) end as gate1b,
      case when review_action is null then null else jsonb_build_object(
        'action', review_action, 'note', review_note, 'decided_at', decided_at) end as review_decision,
      case when route = 'review' and is_representative and not resolved and not pending_hd
        and gate1b_queue <> 'suppressed' then case gate1b_queue when 'likely_male' then 0
          when 'uncertain' then 1 when 'female_audit' then 2 else 3 end else 9 end as queue_priority
    from routed
    order by queue_priority, captured_at desc
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  )
  select coalesce(jsonb_agg(to_jsonb(feed) - 'queue_priority' order by queue_priority, captured_at desc), '[]'::jsonb)
  from feed;
$$;

revoke all on function public.deerid_gate1b_pending(text, text, integer) from public, anon, authenticated;
revoke all on function public.deerid_record_gate1b_batch(text, text, jsonb) from public, anon, authenticated;
revoke all on function public.deerid_record_gate1b_label(uuid, bigint, integer, text, text, text, text, text) from public, anon, authenticated;
revoke all on function public.deerid_gate1b_metrics() from public, anon, authenticated;
grant execute on function public.deerid_gate1b_pending(text, text, integer) to service_role;
grant execute on function public.deerid_record_gate1b_batch(text, text, jsonb) to service_role;
grant execute on function public.deerid_record_gate1b_label(uuid, bigint, integer, text, text, text, text, text) to service_role;
grant execute on function public.deerid_gate1b_metrics() to service_role;
