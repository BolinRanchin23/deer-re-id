-- Trigger one authenticated GPT analysis call whenever a new immutable cloud-HD asset appears.
create extension if not exists pg_net with schema extensions;

create or replace function public.deerid_claim_hd_review(
  p_model_name text,
  p_model_version text,
  p_media_asset_id uuid default null
)
returns jsonb language plpgsql security definer
set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid();
begin
 delete from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes';
 select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
   and (p_media_asset_id is null or a.id=p_media_asset_id)
   and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
   and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)
   and not exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
   and not exists(select 1 from deerid.hd_review_results prior join deerid.hd_review_decisions d on d.hd_review_result_id=prior.id and d.action<>'defer' where prior.media_asset_id=a.id)
 order by a.observed_at for update skip locked limit 1;
 if chosen.id is null then return jsonb_build_object('ok',true,'empty',true); end if;
 insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
 return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;
revoke all on function public.deerid_claim_hd_review(text,text,uuid) from public,anon,authenticated;
grant execute on function public.deerid_claim_hd_review(text,text,uuid) to service_role;

create or replace function deerid.trigger_hd_gpt_analysis()
returns trigger language plpgsql security definer
set search_path=pg_catalog,deerid,extensions,vault,pg_temp as $$
declare trigger_url text; trigger_secret text;
begin
 if new.variant<>'cloud_hd' then return new; end if;
 select decrypted_secret into trigger_url from vault.decrypted_secrets where name='deerid_hd_analysis_trigger_url' order by created_at desc limit 1;
 select decrypted_secret into trigger_secret from vault.decrypted_secrets where name='deerid_hd_analysis_trigger_secret' order by created_at desc limit 1;
 if trigger_url is null or trigger_secret is null then return new; end if;
 perform net.http_post(
   url:=trigger_url,
   headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||trigger_secret),
   body:=jsonb_build_object('media_asset_id',new.id),
   timeout_milliseconds:=5000
 );
 return new;
end $$;
revoke all on function deerid.trigger_hd_gpt_analysis() from public,anon,authenticated;
drop trigger if exists trigger_hd_gpt_analysis on deerid.media_assets;
create trigger trigger_hd_gpt_analysis after insert on deerid.media_assets
for each row when (new.variant='cloud_hd') execute function deerid.trigger_hd_gpt_analysis();
