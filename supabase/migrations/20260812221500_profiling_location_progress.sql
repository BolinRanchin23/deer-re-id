-- Make Profiling location grouping and remaining counts authoritative.
create or replace function public.deerid_hd_review_queue(p_limit integer default 60)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
 select distinct on (r.media_asset_id) r.* from deerid.hd_review_results r
 order by r.media_asset_id,r.created_at desc,r.id desc
), pending as (
 select i.*,r.model_name,r.model_version,r.created_at result_created_at,
   count(*) over(partition by i.hd_review_result_id)::integer instance_count
 from latest_results r join deerid.hd_animal_instances i on i.hd_review_result_id=r.id
 where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
 and not exists(
   select 1 from deerid.hd_animal_instances assigned_i
   join deerid.hd_instance_profile_assignment_events e on e.hd_animal_instance_id=assigned_i.id
   where assigned_i.media_asset_id=r.media_asset_id
 )
 order by r.created_at,i.instance_index
 limit greatest(1,least(coalesce(p_limit,60),120))
)
select coalesce(jsonb_agg(jsonb_build_object(
 'hd_review_result_id',p.hd_review_result_id,'hd_animal_instance_id',p.id,'media_id',p.media_id,'media_asset_id',p.media_asset_id,
 'instance_index',p.instance_index,'instance_count',p.instance_count,
 'bbox',jsonb_build_object('x',p.bbox_x,'y',p.bbox_y,'width',p.bbox_width,'height',p.bbox_height),
 'crop_recipe',p.crop_recipe,'detection_complete',p.detection_complete,'detection_notes',p.detection_notes,
 'model_name',p.model_name,'model_version',p.model_version,'result',p.analysis,'created_at',p.result_created_at,
 'captured_at',m.captured_at,'camera_id',m.camera_id,'camera_name',c.name
) order by p.result_created_at,p.instance_index),'[]'::jsonb)
from pending p join deerid.media m on m.id=p.media_id left join deerid.cameras c on c.id=m.camera_id;
$$;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;

create or replace function public.deerid_hd_review_progress()
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
  select distinct on (r.media_asset_id) r.id,r.media_asset_id
  from deerid.hd_review_results r
  order by r.media_asset_id,r.created_at desc,r.id desc
), current_instances as (
  select i.id,i.media_asset_id,m.camera_id
  from latest_results r
  join deerid.hd_animal_instances i on i.hd_review_result_id=r.id
  join deerid.media m on m.id=i.media_id
), classified_assets as (
  select distinct assigned_i.media_asset_id
  from deerid.hd_instance_profile_assignment_events e
  join deerid.hd_animal_instances assigned_i on assigned_i.id=e.hd_animal_instance_id
  where e.animal_profile_id is not null
), pending as (
  select i.* from current_instances i
  where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
  and not exists(select 1 from classified_assets a where a.media_asset_id=i.media_asset_id)
), totals as (select count(*)::integer value from current_instances),
remaining as (select count(*)::integer value from pending),
by_camera as (
  select coalesce(jsonb_object_agg(camera_id::text,value),'{}'::jsonb) value
  from (select camera_id,count(*)::integer value from pending where camera_id is not null group by camera_id) grouped
)
select jsonb_build_object(
  'total',totals.value,'remaining',remaining.value,
  'completed',greatest(0,totals.value-remaining.value),'by_camera',by_camera.value
) from totals,remaining,by_camera;
$$;
revoke all on function public.deerid_hd_review_progress() from public,anon,authenticated;
grant execute on function public.deerid_hd_review_progress() to service_role;
