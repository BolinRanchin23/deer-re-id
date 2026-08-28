-- Balance model-assisted labeling across cameras and Gate 1 review/archive strata.
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
      m.camera_id, m.captured_at, m.object_path, g.route as gate1_route,
      row_number() over (
        partition by m.camera_id, g.route
        order by m.captured_at desc, m.id
      ) as stratum_rank
    from latest_gate1 g
    join deerid.media m on m.id = g.media_id
    where m.variant = 'cloud_thumbnail'
      and g.route in ('review', 'archive')
      and (g.is_representative or g.route = 'review')
      and not exists (
        select 1 from deerid.gate1b_predictions p
        where p.gate1_assessment_id = g.id
          and p.model_name = p_model_name and p.model_version = p_model_version
      )
  )
  select coalesce(jsonb_agg(
    to_jsonb(chosen) order by chosen.stratum_rank, chosen.camera_id,
      case when chosen.gate1_route = 'review' then 0 else 1 end,
      chosen.captured_at desc
  ), '[]'::jsonb)
  from (
    select media_id, gate1_assessment_id, event_key, camera_id, captured_at,
      object_path, gate1_route, stratum_rank
    from candidates
    order by stratum_rank, camera_id,
      case when gate1_route = 'review' then 0 else 1 end,
      captured_at desc
    limit greatest(1, least(coalesce(p_limit, 20), 60))
  ) chosen;
$$;

revoke all on function public.deerid_gate1b_pending(text, text, integer) from public, anon, authenticated;
grant execute on function public.deerid_gate1b_pending(text, text, integer) to service_role;
