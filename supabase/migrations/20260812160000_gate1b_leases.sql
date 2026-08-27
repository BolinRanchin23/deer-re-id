-- Lease Gate 1B work so duplicate/overlapping Vercel cron delivery cannot duplicate inference cost.
create table if not exists deerid.gate1b_claims (
  gate1_assessment_id bigint primary key references deerid.gate1_assessments(id) on delete restrict,
  model_name text not null,
  model_version text not null,
  claim_token uuid not null,
  claimed_at timestamptz not null default now(),
  unique(claim_token,gate1_assessment_id)
);
alter table deerid.gate1b_claims enable row level security;
grant select,insert,delete on deerid.gate1b_claims to service_role;

create or replace function public.deerid_claim_gate1b_batch(p_model_name text,p_model_version text,p_limit integer default 10)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare token uuid:=gen_random_uuid();items jsonb;
begin
 if length(coalesce(p_model_name,'')) not between 1 and 120 or length(coalesce(p_model_version,'')) not between 1 and 160 then raise exception 'invalid Gate 1B claim'; end if;
 if not pg_try_advisory_xact_lock(hashtextextended('deerid-gate1b-claim',0)) then return jsonb_build_object('ok',true,'empty',true,'busy',true); end if;
 delete from deerid.gate1b_claims where claimed_at<now()-interval '10 minutes';
 with latest_gate1 as (
  select distinct on (ga.media_id) ga.* from deerid.gate1_assessments ga order by ga.media_id,ga.created_at desc,ga.id desc
 ), candidates as (
  select m.id media_id,g.id gate1_assessment_id,g.event_key,m.camera_id,m.captured_at,m.object_path,g.route gate1_route,
   row_number() over(partition by m.camera_id,g.route order by m.captured_at desc,m.id) stratum_rank
  from latest_gate1 g join deerid.media m on m.id=g.media_id
  where m.variant='cloud_thumbnail' and g.route in('review','archive') and (g.is_representative or g.route='review')
   and not exists(select 1 from deerid.gate1b_predictions p where p.gate1_assessment_id=g.id and p.model_name=p_model_name and p.model_version=p_model_version)
   and not exists(select 1 from deerid.gate1b_claims c where c.gate1_assessment_id=g.id)
 ), chosen as (
  select * from candidates order by stratum_rank,gate1_route desc,camera_id,captured_at desc limit greatest(1,least(coalesce(p_limit,10),20))
 ), leased as (
  insert into deerid.gate1b_claims(gate1_assessment_id,model_name,model_version,claim_token)
  select gate1_assessment_id,p_model_name,p_model_version,token from chosen on conflict do nothing returning gate1_assessment_id
 )
 select coalesce(jsonb_agg(to_jsonb(chosen)-'stratum_rank'-'gate1_route' order by chosen.stratum_rank,chosen.camera_id,chosen.captured_at desc),'[]'::jsonb)
 into items from chosen join leased using(gate1_assessment_id);
 return jsonb_build_object('ok',true,'empty',jsonb_array_length(items)=0,'claim_token',token,'items',items);
end $$;
revoke all on function public.deerid_claim_gate1b_batch(text,text,integer) from public,anon,authenticated;
grant execute on function public.deerid_claim_gate1b_batch(text,text,integer) to service_role;

create or replace function public.deerid_complete_gate1b_batch(p_claim_token uuid,p_model_name text,p_model_version text,p_results jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare item jsonb;claimed_count integer;persisted_count integer:=0;unfinished_count integer;
begin
 if jsonb_typeof(p_results)<>'array' or jsonb_array_length(p_results)>20 then raise exception 'invalid Gate 1B completion'; end if;
 perform 1 from deerid.gate1b_claims where claim_token=p_claim_token and model_name=p_model_name and model_version=p_model_version for update;
 select count(*) into claimed_count from deerid.gate1b_claims where claim_token=p_claim_token and model_name=p_model_name and model_version=p_model_version;
 if claimed_count=0 then raise exception 'stale Gate 1B claim'; end if;
 for item in select value from jsonb_array_elements(p_results) loop
  if not exists(select 1 from deerid.gate1b_claims where claim_token=p_claim_token and gate1_assessment_id=(item->>'gate1_assessment_id')::bigint) then raise exception 'unclaimed Gate 1B result'; end if;
  perform public.deerid_record_gate1b_batch(p_model_name,p_model_version,jsonb_build_array(item));
 end loop;
 select count(*) into persisted_count from deerid.gate1b_claims c join deerid.gate1b_predictions p on p.gate1_assessment_id=c.gate1_assessment_id and p.model_name=c.model_name and p.model_version=c.model_version where c.claim_token=p_claim_token;
 unfinished_count:=claimed_count-persisted_count;
 delete from deerid.gate1b_claims where claim_token=p_claim_token;
 return jsonb_build_object('ok',true,'claimed',claimed_count,'persisted',persisted_count,'unfinished',unfinished_count);
end $$;
revoke all on function public.deerid_complete_gate1b_batch(uuid,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.deerid_complete_gate1b_batch(uuid,text,text,jsonb) to service_role;
