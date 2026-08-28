-- Authoritative progress for profiling returned-HD animal instances.
create or replace function public.deerid_hd_review_progress()
returns jsonb
language sql
stable
security definer
set search_path=pg_catalog,public,deerid,pg_temp
as $$
with latest_results as (
  select distinct on (r.media_asset_id) r.id, r.media_asset_id
  from deerid.hd_review_results r
  order by r.media_asset_id, r.created_at desc, r.id desc
), current_instances as (
  select i.id, i.media_asset_id
  from latest_results r
  join deerid.hd_animal_instances i on i.hd_review_result_id = r.id
), classified_assets as (
  select distinct assigned_i.media_asset_id
  from deerid.hd_instance_profile_assignment_events e
  join deerid.hd_animal_instances assigned_i on assigned_i.id = e.hd_animal_instance_id
  where e.animal_profile_id is not null
), remaining as (
  select count(*)::integer as value
  from current_instances i
  where not exists (
    select 1 from deerid.hd_review_decisions d
    where d.hd_animal_instance_id = i.id and d.action <> 'defer'
  )
  and not exists (
    select 1 from classified_assets a where a.media_asset_id = i.media_asset_id
  )
), totals as (
  select count(*)::integer as value from current_instances
)
select jsonb_build_object(
  'total', totals.value,
  'remaining', remaining.value,
  'completed', greatest(0, totals.value - remaining.value)
)
from totals, remaining;
$$;
revoke all on function public.deerid_hd_review_progress() from public,anon,authenticated;
grant execute on function public.deerid_hd_review_progress() to service_role;
