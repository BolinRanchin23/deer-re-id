-- One-shot, auditable authorization to replay the four HD assets quarantined
-- because the serverless runtime lacked the localizer dependency.
create table if not exists deerid.hd_review_retry_authorizations (
  media_asset_id uuid primary key references deerid.media_assets(id) on delete restrict,
  reason text not null check (length(reason) between 1 and 160),
  authorized_at timestamptz not null default now()
);
alter table deerid.hd_review_retry_authorizations enable row level security;
revoke all on deerid.hd_review_retry_authorizations from public,anon,authenticated,service_role;

insert into deerid.hd_review_retry_authorizations(media_asset_id,reason)
select incident.media_asset_id,incident.reason
from (values
 ('8a974195-7014-418e-959b-cb8b04b2171d'::uuid,'serverless localizer dependency incident 2026-08-13'),
 ('d00e93ff-b5ef-45d4-b04e-cdf1cc3151b8'::uuid,'serverless localizer dependency incident 2026-08-13'),
 ('ca2b047b-7f23-4f72-85bf-ee02d42fa4b3'::uuid,'serverless localizer dependency incident 2026-08-13'),
 ('b8011eae-9ccc-4ee3-babb-671bf8119457'::uuid,'serverless localizer dependency incident 2026-08-13')
) as incident(media_asset_id,reason)
join deerid.media_assets asset on asset.id=incident.media_asset_id
on conflict(media_asset_id) do nothing;

create or replace function public.deerid_claim_hd_review(
 p_model_name text,p_model_version text,p_media_asset_id uuid default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid();
begin
 delete from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes';
 select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
 and (p_media_asset_id is null or a.id=p_media_asset_id)
 and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
 and (
   not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)
   or exists(select 1 from deerid.hd_review_retry_authorizations retry where retry.media_asset_id=a.id)
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
 delete from deerid.hd_review_retry_authorizations where media_asset_id=chosen.id;
 insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
 return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;
revoke all on function public.deerid_claim_hd_review(text,text,uuid) from public,anon,authenticated;
grant execute on function public.deerid_claim_hd_review(text,text,uuid) to service_role;
