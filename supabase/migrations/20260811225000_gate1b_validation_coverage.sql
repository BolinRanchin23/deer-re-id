-- Operational coverage report for the model-assisted Gate 1B labeling batch.
create or replace function public.deerid_gate1b_validation_coverage()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid
as $$
  select jsonb_build_object(
    'predictions', count(*)::integer,
    'review_stratum', count(*) filter (where g.route = 'review')::integer,
    'archive_stratum', count(*) filter (where g.route = 'archive')::integer,
    'cameras', count(distinct m.camera_id)::integer,
    'day_color', count(*) filter (where p.lighting = 'day_color')::integer,
    'night_ir', count(*) filter (where p.lighting = 'night_ir')::integer,
    'mixed_groups', count(*) filter (where p.mixed_group)::integer,
    'predicted_axis', count(*) filter (where p.species_label = 'axis')::integer,
    'likely_male', count(*) filter (where p.triage_class = 'likely_male')::integer,
    'uncertain', count(*) filter (where p.triage_class = 'uncertain')::integer,
    'female_candidates', count(*) filter (where p.triage_class = 'female_candidate')::integer
  )
  from deerid.gate1b_predictions p
  join deerid.gate1_assessments g on g.id = p.gate1_assessment_id
  join deerid.media m on m.id = p.media_id
  where p.model_name = 'Ollama-Gemma4-Vision';
$$;

revoke all on function public.deerid_gate1b_validation_coverage() from public, anon, authenticated;
grant execute on function public.deerid_gate1b_validation_coverage() to service_role;
