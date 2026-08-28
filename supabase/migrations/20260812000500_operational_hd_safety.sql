-- Repair operational HD completion semantics and prevent poisoned HD assets from starving the queue.

create table deerid.hd_review_failures (
  id bigint generated always as identity primary key,
  media_asset_id uuid not null references deerid.media_assets(id) on delete restrict,
  error_category text not null check(length(error_category) between 1 and 80),
  created_at timestamptz not null default now(),
  unique(media_asset_id)
);
alter table deerid.hd_review_failures enable row level security;
grant select, insert on deerid.hd_review_failures to service_role;
grant usage, select on sequence deerid.hd_review_failures_id_seq to service_role;
revoke update, delete, truncate on deerid.hd_review_failures from service_role;

create or replace function public.deerid_claim_hd_review(p_model_name text,p_model_version text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid(); begin
  delete from deerid.hd_review_claims where claimed_at < now()-interval '30 minutes';
  select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
  and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
  and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)
  and not exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
  order by a.observed_at for update skip locked limit 1;
  if chosen.id is null then return jsonb_build_object('ok',true,'empty',true); end if;
  insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
  return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;

create or replace function public.deerid_fail_hd_review(p_claim_token uuid,p_error_category text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare asset_id uuid; begin
 if length(coalesce(p_error_category,'')) not between 1 and 80 then raise exception 'invalid HD review failure'; end if;
 select media_asset_id into asset_id from deerid.hd_review_claims where claim_token=p_claim_token for update;
 if asset_id is null then raise exception 'stale HD review claim'; end if;
 insert into deerid.hd_review_failures(media_asset_id,error_category) values(asset_id,p_error_category) on conflict(media_asset_id) do nothing;
 delete from deerid.hd_review_claims where claim_token=p_claim_token;
 return jsonb_build_object('ok',true,'error_category',p_error_category); end $$;

create or replace function public.deerid_complete_hd_request(p_request_token uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare request_row deerid.hd_requests%rowtype; decision_id bigint; advanced_id bigint; operational boolean;
begin
 select * into request_row from deerid.hd_requests where request_token=p_request_token and status='requesting' for update;
 if request_row.id is null then raise exception 'stale HD request token'; end if;
 operational := request_row.priority_reason in ('gate1b_automatic_likely_male','gate1b_audit_correction');
 if operational then
   update deerid.gate1_review_state set pending_hd=false,resolved=true,updated_at=now()
   where gate1_assessment_id=request_row.gate1_assessment_id;
 elsif request_row.requested_by_decision_id is null then
   update deerid.gate1_review_state set pending_hd=false,resolved=true,version=version+1,updated_at=now()
   where gate1_assessment_id=request_row.gate1_assessment_id and version=request_row.review_version and not resolved and pending_hd
   returning gate1_assessment_id into advanced_id;
   if advanced_id is null then raise exception 'stale HD review capability'; end if;
   insert into deerid.review_decisions(media_id,gate1_assessment_id,review_version,action,note)
   values(request_row.media_id,request_row.gate1_assessment_id,request_row.review_version,'request_hd',request_row.pending_note) returning id into decision_id;
 else decision_id:=request_row.requested_by_decision_id; end if;
 update deerid.hd_requests set status='submitted',requested_by_decision_id=decision_id,attempts=attempts+1,
 submitted_at=now(),updated_at=now(),request_token=null,request_started_at=null,last_error=null where id=request_row.id;
 return jsonb_build_object('ok',true,'status','submitted','request_id',request_row.id);
end $$;
