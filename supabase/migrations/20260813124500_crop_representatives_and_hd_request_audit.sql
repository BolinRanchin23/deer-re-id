-- Profile cards use the selected current animal-instance crop; expose a bounded operational HD-request audit.
create or replace function public.deerid_profiles() returns jsonb
language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_assignments as (
 select e.* from deerid.hd_instance_profile_assignment_events e
 where not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)
), current_representatives as (
 select distinct on (r.animal_profile_id) r.* from deerid.profile_representative_events r
 where not exists(select 1 from deerid.profile_representative_events n where n.supersedes_event_id=r.id)
 order by r.animal_profile_id,r.created_at desc,r.id desc
), profile_rows as (
 select ap.id,a.id animal_id,a.display_name,a.species,coalesce(a.sex,'unknown') sex,ap.season_year,
  counts.photo_count,counts.first_seen,counts.last_seen,counts.camera_ids,counts.camera_names,
  previews.items profile_previews,previews.representative_assignment_event_id
 from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
 left join lateral (
  select count(am.media_id)::int photo_count,min(m.captured_at) first_seen,max(m.captured_at) last_seen,
   coalesce(jsonb_agg(distinct m.camera_id) filter(where m.camera_id is not null),'[]'::jsonb) camera_ids,
   coalesce(jsonb_agg(distinct c.name) filter(where c.name is not null),'[]'::jsonb) camera_names
  from deerid.animal_media am join deerid.media m on m.id=am.media_id left join deerid.cameras c on c.id=m.camera_id
  where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
 ) counts on true
 left join lateral (
  select coalesce(jsonb_agg(jsonb_build_object(
    'media_id',picked.media_id,'media_asset_id',picked.media_asset_id,
    'hd_animal_instance_id',picked.hd_animal_instance_id,'assignment_event_id',picked.assignment_event_id,
    'captured_at',picked.captured_at,'bbox',picked.bbox,'crop_recipe',picked.crop_recipe,
    'is_representative',picked.is_representative
  ) order by picked.is_representative desc,picked.captured_at desc),'[]'::jsonb) items,
   max(picked.assignment_event_id) filter(where picked.is_representative) representative_assignment_event_id
  from (
   select i.media_id,i.media_asset_id,i.id hd_animal_instance_id,ce.id assignment_event_id,
    m.captured_at,jsonb_build_object('x',i.bbox_x,'y',i.bbox_y,'width',i.bbox_width,'height',i.bbox_height) bbox,i.crop_recipe,
    (cr.assignment_event_id=ce.id and cr.animal_profile_id=ap.id)::boolean is_representative
   from current_assignments ce
   join deerid.hd_animal_instances i on i.id=ce.hd_animal_instance_id
   join deerid.media m on m.id=i.media_id
   left join current_representatives cr on cr.animal_profile_id=ap.id and cr.assignment_event_id=ce.id
   where ce.animal_profile_id=ap.id
   order by is_representative desc,m.captured_at desc limit 5
  ) picked
 ) previews on true
 where ap.active and a.status='active'
)
select coalesce(jsonb_agg(to_jsonb(profile_rows) order by display_name),'[]'::jsonb) from profile_rows;
$$;
revoke all on function public.deerid_profiles() from public,anon,authenticated;
grant execute on function public.deerid_profiles() to service_role;

create or replace function public.deerid_hd_request_audit_24h() returns jsonb
language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with requests as (
 select r.*,m.provider_photo_id,m.captured_at,m.ingested_at,m.camera_id,c.name camera_name
 from deerid.hd_requests r join deerid.media m on m.id=r.media_id left join deerid.cameras c on c.id=m.camera_id
 where r.created_at>=now()-interval '24 hours'
), rows as (
 select r.*,
  a.id media_asset_id,a.observed_at hd_observed_at,
  result.id hd_review_result_id,result.created_at analyzed_at,
  failure.error_category analysis_failure,
  coalesce(instances.crop_count,0) crop_count,
  coalesce(instances.profile_ready_count,0) profile_ready_count,
  coalesce(instances.assigned_count,0) assigned_count,
  coalesce(instances.rejected_count,0) rejected_count
 from requests r
 left join lateral (
  select ma.* from deerid.media_assets ma where ma.media_id=r.media_id and ma.variant='cloud_hd'
  order by ma.observed_at desc,ma.id desc limit 1
 ) a on true
 left join lateral (
  select rr.* from deerid.hd_review_results rr where rr.media_asset_id=a.id order by rr.created_at desc,rr.id desc limit 1
 ) result on true
 left join lateral (
  select f.error_category from deerid.hd_review_failures f where f.media_asset_id=a.id order by f.created_at desc limit 1
 ) failure on true
 left join lateral (
  select count(*)::int crop_count,
   count(*) filter(where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer'))::int profile_ready_count,
   count(*) filter(where exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id) and e.animal_profile_id is not null))::int assigned_count,
   count(*) filter(where exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action='not_identity_worthy'))::int rejected_count
  from deerid.hd_animal_instances i where i.media_asset_id=a.id
 ) instances on true
)
select jsonb_build_object(
 'as_of',now(),'request_count',(select count(*) from rows),
 'rows',coalesce((select jsonb_agg(jsonb_build_object(
  'request_id',id,'media_id',media_id,'provider_photo_id',provider_photo_id,'camera_id',camera_id,'camera_name',camera_name,
  'captured_at',captured_at,'ingested_at',ingested_at,'requested_at',created_at,'submitted_at',submitted_at,
  'request_status',status,'attempts',attempts,'last_error',last_error,
  'media_asset_id',media_asset_id,'hd_observed_at',hd_observed_at,'hd_review_result_id',hd_review_result_id,
  'analyzed_at',analyzed_at,'analysis_failure',analysis_failure,'crop_count',crop_count,
  'profile_ready_count',profile_ready_count,'assigned_count',assigned_count,'rejected_count',rejected_count,
  'outcome',case when media_asset_id is null then 'not_returned' when hd_review_result_id is null then 'not_analyzed' when crop_count=0 then 'no_crops' when profile_ready_count+assigned_count+rejected_count<crop_count then 'unaccounted_crop' else 'accounted' end
 ) order by created_at,id) from rows),'[]'::jsonb)
);
$$;
revoke all on function public.deerid_hd_request_audit_24h() from public,anon,authenticated;
grant execute on function public.deerid_hd_request_audit_24h() to service_role;
