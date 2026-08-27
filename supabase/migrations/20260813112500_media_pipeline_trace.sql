-- Bounded service-role incident trace for one exact media record.
create or replace function public.deerid_media_pipeline_trace(p_media_id uuid)
returns jsonb language sql stable security definer
set search_path=pg_catalog,public,deerid,pg_temp as $$
select jsonb_build_object(
  'media_id',m.id,
  'captured_at',m.captured_at,
  'variant',m.variant,
  'assets',coalesce((select jsonb_agg(jsonb_build_object(
    'media_asset_id',a.id,'variant',a.variant,'observed_at',a.observed_at,
    'results',coalesce((select jsonb_agg(jsonb_build_object(
      'hd_review_result_id',r.id,'model_name',r.model_name,'model_version',r.model_version,'created_at',r.created_at,
      'animal_count',jsonb_array_length(coalesce(r.result->'animals','[]'::jsonb)),
      'instances',coalesce((select jsonb_agg(jsonb_build_object(
        'hd_animal_instance_id',i.id,'instance_index',i.instance_index,'detection_complete',i.detection_complete,
        'decision',coalesce((select d.action from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer' order by d.created_at desc,d.id desc limit 1),''),
        'assigned',exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id)
      ) order by i.instance_index) from deerid.hd_animal_instances i where i.hd_review_result_id=r.id),'[]'::jsonb)
    ) order by r.created_at desc) from deerid.hd_review_results r where r.media_asset_id=a.id),'[]'::jsonb),
    'failure_count',(select count(*) from deerid.hd_review_failures f where f.media_asset_id=a.id),
    'claimed',exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
  ) order by a.observed_at) from deerid.media_assets a where a.media_id=m.id),'[]'::jsonb)
) from deerid.media m where m.id=p_media_id;
$$;
revoke all on function public.deerid_media_pipeline_trace(uuid) from public,anon,authenticated;
grant execute on function public.deerid_media_pipeline_trace(uuid) to service_role;
