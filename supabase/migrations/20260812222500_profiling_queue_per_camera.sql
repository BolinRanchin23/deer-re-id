-- Return a bounded actionable Profiling slice per camera so every non-empty location has visible work.
create or replace function public.deerid_hd_review_queue(p_limit integer default 25)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
 select distinct on (r.media_asset_id) r.* from deerid.hd_review_results r
 order by r.media_asset_id,r.created_at desc,r.id desc
), pending_all as (
 select i.*,r.model_name,r.model_version,r.created_at result_created_at,m.captured_at,m.camera_id,c.name camera_name,
   count(*) over(partition by i.hd_review_result_id)::integer instance_count,
   row_number() over(partition by m.camera_id order by r.created_at,i.instance_index)::integer camera_rank
 from latest_results r
 join deerid.hd_animal_instances i on i.hd_review_result_id=r.id
 join deerid.media m on m.id=i.media_id
 left join deerid.cameras c on c.id=m.camera_id
 where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
 and not exists(
   select 1 from deerid.hd_animal_instances assigned_i
   join deerid.hd_instance_profile_assignment_events e on e.hd_animal_instance_id=assigned_i.id
   where assigned_i.media_asset_id=r.media_asset_id
 )
), pending as (
 select * from pending_all where camera_rank<=greatest(1,least(coalesce(p_limit,25),25))
)
select coalesce(jsonb_agg(jsonb_build_object(
 'hd_review_result_id',p.hd_review_result_id,'hd_animal_instance_id',p.id,'media_id',p.media_id,'media_asset_id',p.media_asset_id,
 'instance_index',p.instance_index,'instance_count',p.instance_count,
 'bbox',jsonb_build_object('x',p.bbox_x,'y',p.bbox_y,'width',p.bbox_width,'height',p.bbox_height),
 'crop_recipe',p.crop_recipe,'detection_complete',p.detection_complete,'detection_notes',p.detection_notes,
 'model_name',p.model_name,'model_version',p.model_version,'result',p.analysis,'created_at',p.result_created_at,
 'captured_at',p.captured_at,'camera_id',p.camera_id,'camera_name',p.camera_name
) order by p.result_created_at,p.instance_index),'[]'::jsonb) from pending p;
$$;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;
