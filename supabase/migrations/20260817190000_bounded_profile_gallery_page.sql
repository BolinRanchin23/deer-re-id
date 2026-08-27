-- Keep normal profile growth from breaking the dashboard bootstrap.
-- Gallery photos are loaded only after a user opens one exact profile.
create or replace function public.deerid_profile_gallery_page(
  p_profile_id uuid,
  p_limit integer default 24
) returns jsonb
language sql stable security definer
set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_events as (
  select distinct on (e.hd_animal_instance_id) e.*
  from deerid.hd_instance_profile_assignment_events e
  order by e.hd_animal_instance_id,e.created_at desc,e.id desc
), picked as (
  select am.animal_profile_id,am.media_id,m.captured_at,m.camera_id,c.name camera_name,
    ce.id assignment_event_id,i.id hd_animal_instance_id,i.media_asset_id,
    case when i.id is null then null else jsonb_build_object(
      'x',i.bbox_x,'y',i.bbox_y,'width',i.bbox_width,'height',i.bbox_height
    ) end bbox,
    i.crop_recipe
  from deerid.animal_media am
  join deerid.media m on m.id=am.media_id
  left join deerid.cameras c on c.id=m.camera_id
  left join lateral (
    select e.*
    from current_events e
    join deerid.hd_animal_instances candidate_i on candidate_i.id=e.hd_animal_instance_id
    where e.animal_profile_id=am.animal_profile_id
      and candidate_i.media_id=am.media_id
    order by e.created_at desc,e.id desc
    limit 1
  ) ce on true
  left join deerid.hd_animal_instances i on i.id=ce.hd_animal_instance_id
  where am.confirmation_status='confirmed'
    and am.animal_profile_id=p_profile_id
  order by m.captured_at desc,am.media_id
  limit greatest(1,least(coalesce(p_limit,24),60))
)
select coalesce(jsonb_agg(jsonb_build_object(
  'assignment_event_id',assignment_event_id,
  'hd_animal_instance_id',hd_animal_instance_id,
  'animal_profile_id',animal_profile_id,
  'media_id',media_id,
  'media_asset_id',media_asset_id,
  'captured_at',captured_at,
  'camera_id',camera_id,
  'camera_name',camera_name,
  'bbox',bbox,
  'crop_recipe',crop_recipe
) order by captured_at desc,media_id),'[]'::jsonb)
from picked;
$$;

revoke all on function public.deerid_profile_gallery_page(uuid,integer)
  from public,anon,authenticated;
grant execute on function public.deerid_profile_gallery_page(uuid,integer)
  to service_role;
