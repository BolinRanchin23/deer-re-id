-- Add bounded confirmed profile previews and make the latest HD prompt result authoritative per asset.

create or replace function public.deerid_profiles()
returns jsonb language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
select coalesce(jsonb_agg(jsonb_build_object(
  'id',ap.id,'animal_id',a.id,'display_name',a.display_name,'species',a.species,
  'sex',coalesce(a.sex,'unknown'),'season_year',ap.season_year,
  'photo_count',coalesce(counts.photo_count,0),'profile_previews',coalesce(previews.items,'[]'::jsonb)
) order by a.display_name,ap.season_year desc,ap.id),'[]'::jsonb)
from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
left join lateral (
 select count(*)::integer photo_count from deerid.animal_media am
 where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
) counts on true
left join lateral (
 select coalesce(jsonb_agg(jsonb_build_object('media_id',picked.media_id,'captured_at',picked.captured_at)
   order by picked.captured_at desc),'[]'::jsonb) items
 from (
   select am.media_id,m.captured_at from deerid.animal_media am join deerid.media m on m.id=am.media_id
   where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
   order by m.captured_at desc limit 5
 ) picked
) previews on true
where a.status='active' and ap.active;
$$;
revoke all on function public.deerid_profiles() from public,anon,authenticated;
grant execute on function public.deerid_profiles() to service_role;

create or replace function public.deerid_claim_hd_review(p_model_name text,p_model_version text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid(); begin
 delete from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes';
 select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
 and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
 and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)
 and not exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
 and not exists(select 1 from deerid.hd_review_results prior join deerid.hd_review_decisions d on d.hd_review_result_id=prior.id and d.action<>'defer' where prior.media_asset_id=a.id)
 order by a.observed_at for update skip locked limit 1;
 if chosen.id is null then return jsonb_build_object('ok',true,'empty',true); end if;
 insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
 return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;

create or replace function public.deerid_hd_review_queue(p_limit integer default 60)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest as (
 select distinct on (r.media_asset_id) r.* from deerid.hd_review_results r
 where not exists(select 1 from deerid.hd_review_decisions d where d.hd_review_result_id=r.id and d.action<>'defer')
 order by r.media_asset_id,r.created_at desc,r.id desc
), picked as (select * from latest order by created_at limit greatest(1,least(coalesce(p_limit,60),120)))
select coalesce(jsonb_agg(jsonb_build_object('hd_review_result_id',r.id,'media_id',r.media_id,'media_asset_id',r.media_asset_id,
 'model_name',r.model_name,'model_version',r.model_version,'result',r.result,'created_at',r.created_at,
 'captured_at',m.captured_at,'camera_name',c.name) order by r.created_at),'[]'::jsonb)
from picked r join deerid.media m on m.id=r.media_id left join deerid.cameras c on c.id=m.camera_id;
$$;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;
