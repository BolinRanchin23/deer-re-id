-- Fail-closed Gate 1B policy: pin validation to an exact model version,
-- require both target species, and disable suppression after any validation regression.

alter table deerid.gate1b_policy
  add column model_name text not null default 'Ollama-Gemma4-Vision',
  add column model_version text not null default 'gemma4-e4b-c6eb396dbd59@prompt-2026-08-11.1',
  add column minimum_whitetail_labels integer not null default 10 check (minimum_whitetail_labels >= 1),
  add column minimum_axis_labels integer not null default 10 check (minimum_axis_labels >= 1),
  add column minimum_whitetail_buck_events integer not null default 10 check (minimum_whitetail_buck_events >= 1),
  add column minimum_axis_buck_events integer not null default 5 check (minimum_axis_buck_events >= 1);

create or replace function deerid.gate1b_model_ready(
  p_model_name text,
  p_model_version text,
  p_minimum_labels integer,
  p_minimum_buck_events integer,
  p_minimum_whitetail_labels integer,
  p_minimum_axis_labels integer,
  p_minimum_whitetail_buck_events integer,
  p_minimum_axis_buck_events integer,
  p_required_buck_recall double precision
)
returns boolean
language sql stable security definer
set search_path = pg_catalog, public, deerid
as $$
  with latest_labels as (
    select distinct on (hl.gate1_assessment_id) hl.*
    from deerid.gate1b_human_labels hl
    order by hl.gate1_assessment_id, hl.created_at desc, hl.id desc
  ), truth as (
    select
      count(*)::integer as labels,
      count(distinct m.camera_id)::integer as cameras,
      count(*) filter (where gp.lighting = 'day_color')::integer as day_labels,
      count(*) filter (where gp.lighting = 'night_ir')::integer as ir_labels,
      count(*) filter (where hl.species_label = 'whitetail')::integer as whitetail_labels,
      count(*) filter (where hl.species_label = 'axis')::integer as axis_labels,
      count(*) filter (
        where hl.visible_antler = 'yes' or hl.probable_male = 'yes'
      )::integer as buck_events,
      count(*) filter (
        where (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
          and gp.triage_class <> 'female_candidate'
      )::integer as retained_buck_events,
      count(*) filter (
        where hl.species_label = 'whitetail'
          and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
      )::integer as whitetail_buck_events,
      count(*) filter (
        where hl.species_label = 'whitetail'
          and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
          and gp.triage_class <> 'female_candidate'
      )::integer as retained_whitetail_buck_events,
      count(*) filter (
        where hl.species_label = 'axis'
          and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
      )::integer as axis_buck_events,
      count(*) filter (
        where hl.species_label = 'axis'
          and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
          and gp.triage_class <> 'female_candidate'
      )::integer as retained_axis_buck_events
    from latest_labels hl
    join deerid.gate1b_predictions gp
      on gp.gate1_assessment_id = hl.gate1_assessment_id
      and gp.model_name = p_model_name and gp.model_version = p_model_version
    join deerid.media m on m.id = hl.media_id
  )
  select
    labels >= p_minimum_labels
    and cameras >= 4
    and day_labels > 0 and ir_labels > 0
    and whitetail_labels >= p_minimum_whitetail_labels
    and axis_labels >= p_minimum_axis_labels
    and buck_events >= p_minimum_buck_events
    and whitetail_buck_events >= p_minimum_whitetail_buck_events
    and axis_buck_events >= p_minimum_axis_buck_events
    and retained_buck_events::double precision / nullif(buck_events, 0) >= p_required_buck_recall
    and retained_whitetail_buck_events::double precision / nullif(whitetail_buck_events, 0) >= p_required_buck_recall
    and retained_axis_buck_events::double precision / nullif(axis_buck_events, 0) >= p_required_buck_recall
  from truth;
$$;

create or replace function deerid.guard_gate1b_policy()
returns trigger language plpgsql security definer
set search_path = pg_catalog, public, deerid
as $$
begin
  if new.suppression_enabled and not deerid.gate1b_model_ready(
    new.model_name, new.model_version, new.minimum_labels,
    new.minimum_buck_events, new.minimum_whitetail_labels,
    new.minimum_axis_labels, new.minimum_whitetail_buck_events,
    new.minimum_axis_buck_events, new.required_buck_recall
  ) then
    raise exception 'Gate 1B suppression validation gate is not satisfied';
  end if;
  return new;
end;
$$;

drop trigger if exists gate1b_policy_fail_closed on deerid.gate1b_policy;
create trigger gate1b_policy_fail_closed before insert or update on deerid.gate1b_policy
for each row execute function deerid.guard_gate1b_policy();

create or replace function deerid.disable_gate1b_after_label_regression()
returns trigger language plpgsql security definer
set search_path = pg_catalog, public, deerid
as $$
begin
  update deerid.gate1b_policy p set suppression_enabled = false
  where p.singleton and p.suppression_enabled
    and not deerid.gate1b_model_ready(
      p.model_name, p.model_version, p.minimum_labels,
      p.minimum_buck_events, p.minimum_whitetail_labels,
      p.minimum_axis_labels, p.minimum_whitetail_buck_events,
      p.minimum_axis_buck_events, p.required_buck_recall
    );
  return new;
end;
$$;

drop trigger if exists gate1b_label_rechecks_policy on deerid.gate1b_human_labels;
create trigger gate1b_label_rechecks_policy after insert on deerid.gate1b_human_labels
for each statement execute function deerid.disable_gate1b_after_label_regression();

create or replace function public.deerid_gate1b_validation_state()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid
as $$
  with policy as (select * from deerid.gate1b_policy where singleton),
  latest_labels as (
    select distinct on (hl.gate1_assessment_id) hl.*
    from deerid.gate1b_human_labels hl
    order by hl.gate1_assessment_id, hl.created_at desc, hl.id desc
  ), truth as (
    select
      count(hl.id)::integer as human_labels,
      count(distinct m.camera_id)::integer as labeled_cameras,
      count(*) filter (where gp.lighting = 'day_color')::integer as labeled_day,
      count(*) filter (where gp.lighting = 'night_ir')::integer as labeled_ir,
      count(*) filter (where hl.species_label = 'whitetail')::integer as labeled_whitetail,
      count(*) filter (where hl.species_label = 'axis')::integer as labeled_axis,
      count(*) filter (where hl.visible_antler = 'yes' or hl.probable_male = 'yes')::integer as buck_events,
      count(*) filter (where (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
        and gp.triage_class <> 'female_candidate')::integer as retained_buck_events,
      count(*) filter (where hl.species_label = 'whitetail'
        and (hl.visible_antler = 'yes' or hl.probable_male = 'yes'))::integer as whitetail_buck_events,
      count(*) filter (where hl.species_label = 'whitetail'
        and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
        and gp.triage_class <> 'female_candidate')::integer as retained_whitetail_buck_events,
      count(*) filter (where hl.species_label = 'axis'
        and (hl.visible_antler = 'yes' or hl.probable_male = 'yes'))::integer as axis_buck_events,
      count(*) filter (where hl.species_label = 'axis'
        and (hl.visible_antler = 'yes' or hl.probable_male = 'yes')
        and gp.triage_class <> 'female_candidate')::integer as retained_axis_buck_events
    from policy
    left join latest_labels hl on true
    left join deerid.gate1b_predictions gp
      on gp.gate1_assessment_id = hl.gate1_assessment_id
      and gp.model_name = policy.model_name and gp.model_version = policy.model_version
    left join deerid.media m on m.id = hl.media_id
    where hl.id is null or gp.id is not null
  )
  select jsonb_build_object(
    'model_name', policy.model_name,
    'model_version', policy.model_version,
    'human_labels', truth.human_labels,
    'labeled_cameras', truth.labeled_cameras,
    'labeled_day', truth.labeled_day,
    'labeled_ir', truth.labeled_ir,
    'labeled_whitetail', truth.labeled_whitetail,
    'labeled_axis', truth.labeled_axis,
    'labeled_buck_events', truth.buck_events,
    'whitetail_buck_events', truth.whitetail_buck_events,
    'axis_buck_events', truth.axis_buck_events,
    'buck_retention_recall', case when truth.buck_events > 0 then
      truth.retained_buck_events::double precision / truth.buck_events else null end,
    'whitetail_buck_retention_recall', case when truth.whitetail_buck_events > 0 then
      truth.retained_whitetail_buck_events::double precision / truth.whitetail_buck_events else null end,
    'axis_buck_retention_recall', case when truth.axis_buck_events > 0 then
      truth.retained_axis_buck_events::double precision / truth.axis_buck_events else null end,
    'suppression_ready', deerid.gate1b_model_ready(
      policy.model_name, policy.model_version, policy.minimum_labels,
      policy.minimum_buck_events, policy.minimum_whitetail_labels,
      policy.minimum_axis_labels, policy.minimum_whitetail_buck_events,
      policy.minimum_axis_buck_events, policy.required_buck_recall),
    'suppression_enabled', policy.suppression_enabled,
    'minimum_labels', policy.minimum_labels,
    'minimum_buck_events', policy.minimum_buck_events,
    'minimum_whitetail_labels', policy.minimum_whitetail_labels,
    'minimum_axis_labels', policy.minimum_axis_labels,
    'minimum_whitetail_buck_events', policy.minimum_whitetail_buck_events,
    'minimum_axis_buck_events', policy.minimum_axis_buck_events,
    'required_buck_recall', policy.required_buck_recall,
    'female_audit_percent', policy.female_audit_percent
  ) from policy cross join truth;
$$;

create or replace function public.deerid_gate1b_metrics()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid
as $$
  with policy as (select * from deerid.gate1b_policy where singleton), p as (
    select count(*)::integer as predictions,
      count(distinct m.camera_id)::integer as prediction_cameras,
      count(*) filter (where gp.species_label = 'whitetail')::integer as predicted_whitetail,
      count(*) filter (where gp.species_label = 'axis')::integer as predicted_axis,
      count(*) filter (where gp.species_label = 'other_deer')::integer as predicted_other_deer,
      count(*) filter (where gp.species_label = 'non_deer')::integer as predicted_non_deer,
      count(*) filter (where gp.lighting = 'day_color')::integer as predicted_day,
      count(*) filter (where gp.lighting = 'night_ir')::integer as predicted_ir,
      count(*) filter (where gp.mixed_group)::integer as predicted_mixed_groups,
      count(*) filter (where gp.triage_class = 'likely_male')::integer as likely_male,
      count(*) filter (where gp.triage_class = 'uncertain')::integer as uncertain,
      count(*) filter (where gp.triage_class = 'female_candidate')::integer as female_candidates
    from policy
    left join deerid.gate1b_predictions gp
      on gp.model_name = policy.model_name and gp.model_version = policy.model_version
    left join deerid.media m on m.id = gp.media_id
  ), state as (select public.deerid_gate1b_validation_state() as value)
  select state.value || jsonb_build_object(
    'predictions', p.predictions, 'prediction_cameras', p.prediction_cameras,
    'predicted_whitetail', p.predicted_whitetail, 'predicted_axis', p.predicted_axis,
    'predicted_other_deer', p.predicted_other_deer, 'predicted_non_deer', p.predicted_non_deer,
    'predicted_day', p.predicted_day, 'predicted_ir', p.predicted_ir,
    'predicted_mixed_groups', p.predicted_mixed_groups,
    'likely_male', p.likely_male, 'uncertain', p.uncertain,
    'female_candidates', p.female_candidates,
    'buck_recall', state.value -> 'buck_retention_recall'
  ) from p cross join state;
$$;

create or replace function public.deerid_record_gate1b_batch(
  p_model_name text, p_model_version text, p_results jsonb
)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare item jsonb; inserted_count integer := 0; assessment record;
begin
  if p_model_name is null or length(p_model_name) not between 1 and 120
     or p_model_version is null or length(p_model_version) not between 1 and 160
     or jsonb_typeof(p_results) <> 'array' or jsonb_array_length(p_results) > 60 then
    raise exception 'invalid Gate 1B batch';
  end if;
  for item in select value from jsonb_array_elements(p_results) loop
    select g.media_id, g.event_key into assessment
    from deerid.gate1_assessments g
    where g.id = (item->>'gate1_assessment_id')::bigint
      and g.id = (select newest.id from deerid.gate1_assessments newest
        where newest.media_id = g.media_id order by newest.created_at desc, newest.id desc limit 1);
    if not found or assessment.media_id <> (item->>'media_id')::uuid
      or assessment.event_key is distinct from item->>'event_key' then
      raise exception 'Gate 1B result does not match latest Gate 1 assessment';
    end if;
    if length(coalesce(item->>'reason', '')) not between 1 and 500
      or octet_length(coalesce(item->'raw_output', '{}'::jsonb)::text) > 16000 then
      raise exception 'invalid Gate 1B evidence';
    end if;
    if (item->>'triage_class') = 'female_candidate' and (
      coalesce((item->>'animal_count')::integer, 0) < 1
      or item->>'species_label' not in ('whitetail', 'axis')
      or item->>'visible_antler' <> 'no' or item->>'probable_male' <> 'no'
      or item->>'head_visibility' <> 'full'
      or coalesce((item->>'all_animals_assessed')::boolean, false) is not true
    ) then raise exception 'unsafe female candidate'; end if;
    if coalesce((item->>'model_failure')::boolean, false)
      and item->>'triage_class' <> 'uncertain' then
      raise exception 'model failure cannot leave uncertainty';
    end if;
    if coalesce((item->>'hd_recommended')::boolean, false)
      and item->>'triage_class' <> 'likely_male' then
      raise exception 'unsafe HD recommendation';
    end if;
    insert into deerid.gate1b_predictions (
      media_id, gate1_assessment_id, event_key, model_name, model_version,
      species_label, visible_antler, probable_male, head_visibility, lighting,
      animal_count, mixed_group, all_animals_assessed, triage_class,
      hd_recommended, model_failure, reason, raw_output
    ) values (
      assessment.media_id, (item->>'gate1_assessment_id')::bigint,
      assessment.event_key, p_model_name, p_model_version,
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

create or replace function public.deerid_enable_gate1b_suppression()
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, deerid
as $$
declare updated deerid.gate1b_policy;
begin
  update deerid.gate1b_policy set suppression_enabled = true
  where singleton returning * into updated;
  return jsonb_build_object('ok', true, 'model_name', updated.model_name,
    'model_version', updated.model_version, 'suppression_enabled', updated.suppression_enabled);
end;
$$;

revoke insert, update, delete, truncate on deerid.gate1b_predictions from service_role;
revoke insert, update, delete, truncate on deerid.gate1b_human_labels from service_role;
revoke all on function deerid.gate1b_model_ready(text, text, integer, integer, integer, integer, integer, integer, double precision) from public, anon, authenticated;
revoke all on function deerid.guard_gate1b_policy() from public, anon, authenticated;
revoke all on function deerid.disable_gate1b_after_label_regression() from public, anon, authenticated;
revoke all on function public.deerid_gate1b_validation_state() from public, anon, authenticated;
revoke all on function public.deerid_enable_gate1b_suppression() from public, anon, authenticated;
grant execute on function public.deerid_gate1b_validation_state() to service_role;
grant execute on function public.deerid_enable_gate1b_suppression() to service_role;
