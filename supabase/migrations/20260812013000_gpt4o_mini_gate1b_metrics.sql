-- Report Gate 1B validation metrics for the new GPT-4o mini evidence version.
create or replace function public.deerid_gate1b_metrics()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid
as $$
  with selected as (
    select * from deerid.gate1b_predictions gp
    where gp.model_name = 'OpenAI-GPT-4o-mini-Vision'
      and gp.model_version = 'gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'
  ), p as (
    select count(*)::integer predictions, count(distinct m.camera_id)::integer prediction_cameras,
      count(*) filter(where gp.species_label='whitetail')::integer predicted_whitetail,
      count(*) filter(where gp.species_label='axis')::integer predicted_axis,
      count(*) filter(where gp.species_label='other_deer')::integer predicted_other_deer,
      count(*) filter(where gp.species_label='non_deer')::integer predicted_non_deer,
      count(*) filter(where gp.lighting='day_color')::integer predicted_day,
      count(*) filter(where gp.lighting='night_ir')::integer predicted_ir,
      count(*) filter(where gp.mixed_group)::integer predicted_mixed_groups,
      count(*) filter(where gp.triage_class='likely_male')::integer likely_male,
      count(*) filter(where gp.triage_class='uncertain')::integer uncertain,
      count(*) filter(where gp.triage_class='female_candidate')::integer female_candidates
    from selected gp join deerid.media m on m.id=gp.media_id
  ), latest_labels as (
    select distinct on (gate1_assessment_id) * from deerid.gate1b_human_labels
    order by gate1_assessment_id,created_at desc,id desc
  ), truth as (
    select count(*)::integer human_labels,
      count(*) filter(where hl.visible_antler='yes' or hl.probable_male='yes')::integer buck_events,
      count(*) filter(where (hl.visible_antler='yes' or hl.probable_male='yes') and gp.triage_class<>'female_candidate')::integer recalled_buck_events,
      count(distinct m.camera_id)::integer labeled_cameras,
      count(*) filter(where gp.lighting='day_color')::integer labeled_day,
      count(*) filter(where gp.lighting='night_ir')::integer labeled_ir,
      count(*) filter(where hl.species_label='axis')::integer labeled_axis
    from latest_labels hl join selected gp on gp.gate1_assessment_id=hl.gate1_assessment_id join deerid.media m on m.id=hl.media_id
  ), policy as (select * from deerid.gate1b_policy where singleton)
  select jsonb_build_object(
    'model_name','OpenAI-GPT-4o-mini-Vision','model_version','gpt-4o-mini-2024-07-18@prompt-2026-08-12.1',
    'predictions',p.predictions,'prediction_cameras',p.prediction_cameras,'predicted_whitetail',p.predicted_whitetail,
    'predicted_axis',p.predicted_axis,'predicted_other_deer',p.predicted_other_deer,'predicted_non_deer',p.predicted_non_deer,
    'predicted_day',p.predicted_day,'predicted_ir',p.predicted_ir,'predicted_mixed_groups',p.predicted_mixed_groups,
    'likely_male',p.likely_male,'uncertain',p.uncertain,'female_candidates',p.female_candidates,
    'human_labels',t.human_labels,'labeled_buck_events',t.buck_events,'labeled_cameras',t.labeled_cameras,
    'labeled_day',t.labeled_day,'labeled_ir',t.labeled_ir,'labeled_axis',t.labeled_axis,
    'buck_recall',case when t.buck_events>0 then t.recalled_buck_events::double precision/t.buck_events else null end,
    'suppression_ready',(t.human_labels>=policy.minimum_labels and t.buck_events>=policy.minimum_buck_events and t.labeled_cameras>=4 and t.labeled_day>0 and t.labeled_ir>0 and t.recalled_buck_events::double precision/nullif(t.buck_events,0)>=policy.required_buck_recall),
    'suppression_enabled',policy.suppression_enabled,'minimum_labels',policy.minimum_labels,'minimum_buck_events',policy.minimum_buck_events,
    'required_buck_recall',policy.required_buck_recall,'female_audit_percent',policy.female_audit_percent)
  from p cross join truth t cross join policy;
$$;
revoke all on function public.deerid_gate1b_metrics() from public,anon,authenticated;
grant execute on function public.deerid_gate1b_metrics() to service_role;
