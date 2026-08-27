-- Let a new pinned GPT crop/description version retry assets that have older usable analysis,
-- while continuing to quarantine HD assets that have never produced any result.
create or replace function public.deerid_claim_hd_review(
 p_model_name text,p_model_version text,p_media_asset_id uuid default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid();
begin
 delete from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes';
 select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
 and (p_media_asset_id is null or a.id=p_media_asset_id)
 and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
 and not exists(
   select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id
   and not exists(select 1 from deerid.hd_review_results usable where usable.media_asset_id=a.id)
 )
 and not exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
 and not exists(
   select 1 from deerid.hd_animal_instances prior_i
   join lateral (
     select e.animal_profile_id from deerid.hd_instance_profile_assignment_events e
     where e.hd_animal_instance_id=prior_i.id order by e.created_at desc,e.id desc limit 1
   ) current_event on true
   where prior_i.media_asset_id=a.id and current_event.animal_profile_id is not null
 )
 order by a.observed_at for update skip locked limit 1;
 if chosen.id is null then return jsonb_build_object('ok',true,'empty',true); end if;
 insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
 return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;
revoke all on function public.deerid_claim_hd_review(text,text,uuid) from public,anon,authenticated;
grant execute on function public.deerid_claim_hd_review(text,text,uuid) to service_role;
