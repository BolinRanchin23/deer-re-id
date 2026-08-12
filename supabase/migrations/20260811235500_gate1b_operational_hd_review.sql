-- Explicit operational override: Gemma Gate 1B drives thumbnail routing and HD requests.
-- Model evidence, automatic decisions, and later human corrections remain separate ledgers.

alter table deerid.gate1b_policy
  add column if not exists automatic_hd_enabled boolean not null default false,
  add column if not exists operating_mode text not null default 'validation'
    check (operating_mode in ('validation', 'model_operational'));

create table deerid.gate1b_automation_events (
  id bigint generated always as identity primary key,
  prediction_id bigint not null unique references deerid.gate1b_predictions(id) on delete restrict,
  media_id uuid not null references deerid.media(id) on delete restrict,
  gate1_assessment_id bigint not null references deerid.gate1_assessments(id) on delete restrict,
  action text not null check (action in ('auto_request_hd', 'auto_suppress_female')),
  policy_version text not null,
  model_name text not null,
  model_version text not null,
  created_at timestamptz not null default now()
);
create index gate1b_automation_events_action_idx on deerid.gate1b_automation_events(action, created_at desc);
alter table deerid.gate1b_automation_events enable row level security;
grant select, insert on deerid.gate1b_automation_events to service_role;
grant usage, select on sequence deerid.gate1b_automation_events_id_seq to service_role;
revoke update, delete, truncate on deerid.gate1b_automation_events from service_role;

create table deerid.gate1b_automation_labels (
  id bigint generated always as identity primary key,
  automation_event_id bigint not null references deerid.gate1b_automation_events(id) on delete restrict,
  supersedes_id bigint references deerid.gate1b_automation_labels(id) on delete restrict,
  verdict text not null check (verdict in ('correct', 'should_have_requested_hd', 'incorrect_male_or_antler')),
  note text check (note is null or length(note) <= 500),
  created_at timestamptz not null default now()
);
create index gate1b_automation_labels_event_idx on deerid.gate1b_automation_labels(automation_event_id, created_at desc, id desc);
alter table deerid.gate1b_automation_labels enable row level security;
grant select, insert on deerid.gate1b_automation_labels to service_role;
grant usage, select on sequence deerid.gate1b_automation_labels_id_seq to service_role;
revoke update, delete, truncate on deerid.gate1b_automation_labels from service_role;

create or replace function deerid.reject_gate1b_automation_mutation()
returns trigger language plpgsql set search_path = pg_catalog, deerid, pg_temp as $$
begin raise exception 'Gate 1B automation evidence is append-only'; end; $$;
create trigger gate1b_automation_events_append_only before update or delete on deerid.gate1b_automation_events
for each row execute function deerid.reject_gate1b_automation_mutation();
create trigger gate1b_automation_labels_append_only before update or delete on deerid.gate1b_automation_labels
for each row execute function deerid.reject_gate1b_automation_mutation();
revoke all on function deerid.reject_gate1b_automation_mutation() from public, anon, authenticated;

create or replace function deerid.apply_gate1b_automation_prediction(p_prediction_id bigint)
returns void language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
declare pol deerid.gate1b_policy%rowtype; prediction deerid.gate1b_predictions%rowtype; chosen_action text;
begin
  select * into prediction from deerid.gate1b_predictions where id=p_prediction_id;
  if prediction.id is null then return; end if;
  select * into pol from deerid.gate1b_policy where singleton for update;
  if pol.operating_mode <> 'model_operational' then return; end if;
  if prediction.model_name <> pol.model_name or prediction.model_version <> pol.model_version then return; end if;
  if prediction.triage_class = 'likely_male' and pol.automatic_hd_enabled then
    chosen_action := 'auto_request_hd';
    insert into deerid.hd_requests(media_id, gate1_assessment_id, priority, priority_reason)
    values(prediction.media_id, prediction.gate1_assessment_id, 100, 'gate1b_automatic_likely_male')
    on conflict(media_id) do nothing;
  elsif prediction.triage_class = 'female_candidate' and pol.suppression_enabled = true then
    chosen_action := 'auto_suppress_female';
  else
    return;
  end if;
  insert into deerid.gate1b_automation_events(
    prediction_id, media_id, gate1_assessment_id, action, policy_version, model_name, model_version
  ) values(prediction.id, prediction.media_id, prediction.gate1_assessment_id, chosen_action, pol.policy_version, prediction.model_name, prediction.model_version)
  on conflict(prediction_id) do nothing;
  insert into deerid.gate1_review_state(gate1_assessment_id, version, resolved, pending_hd, updated_at)
  values(prediction.gate1_assessment_id, 1, true, chosen_action = 'auto_request_hd', now())
  on conflict(gate1_assessment_id) do update set
    version = deerid.gate1_review_state.version + 1,
    resolved = true,
    pending_hd = chosen_action = 'auto_request_hd',
    updated_at = now()
  where not deerid.gate1_review_state.resolved;
end; $$;
revoke all on function deerid.apply_gate1b_automation_prediction(bigint) from public, anon, authenticated;

create or replace function deerid.deerid_apply_gate1b_automation()
returns trigger language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
begin
  perform deerid.apply_gate1b_automation_prediction(new.id);
  return new;
end; $$;
revoke all on function deerid.deerid_apply_gate1b_automation() from public, anon, authenticated;
drop trigger if exists gate1b_prediction_automation on deerid.gate1b_predictions;
create trigger gate1b_prediction_automation after insert on deerid.gate1b_predictions
for each row execute function deerid.deerid_apply_gate1b_automation();

-- User-authorized operational mode deliberately bypasses the validation guard and auto-disable hooks.
drop trigger if exists gate1b_policy_fail_closed on deerid.gate1b_policy;
drop trigger if exists gate1b_label_rechecks_policy on deerid.gate1b_human_labels;
drop trigger if exists gate1b_prediction_rechecks_policy on deerid.gate1b_predictions;
update deerid.gate1b_policy set
  suppression_enabled = true,
  automatic_hd_enabled = true,
  operating_mode = 'model_operational',
  policy_version = 'gate1b-gemma-operational-2026-08-11.1',
  updated_at = now()
where singleton;

-- Apply the explicit policy to historical pinned predictions exactly once.
do $$ declare prediction_id bigint; begin
  for prediction_id in
    select p.id from deerid.gate1b_predictions p join deerid.gate1b_policy pol on pol.singleton
    where p.model_name = pol.model_name and p.model_version = pol.model_version
    order by p.id
  loop perform deerid.apply_gate1b_automation_prediction(prediction_id); end loop;
end $$;

create or replace function public.deerid_gate1b_automation_audit(p_limit integer default 120)
returns jsonb language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
with latest_label as (
  select distinct on (automation_event_id) * from deerid.gate1b_automation_labels
  order by automation_event_id, created_at desc, id desc
)
select coalesce(jsonb_agg(jsonb_build_object(
  'automation_event_id', e.id, 'media_id', e.media_id, 'action', e.action,
  'policy_version', e.policy_version, 'model_name', e.model_name, 'model_version', e.model_version,
  'created_at', e.created_at, 'captured_at', m.captured_at, 'camera_name', c.name,
  'prediction', jsonb_build_object('species_label', p.species_label, 'visible_antler', p.visible_antler,
    'probable_male', p.probable_male, 'head_visibility', p.head_visibility,
    'triage_class', p.triage_class, 'reason', p.reason),
  'human_verdict', l.verdict, 'human_note', l.note,
  'human_labeled_at', l.created_at
) order by e.created_at desc), '[]'::jsonb)
from (select * from deerid.gate1b_automation_events order by created_at desc limit greatest(1, least(coalesce(p_limit,120),500))) e
join deerid.media m on m.id=e.media_id left join deerid.cameras c on c.id=m.camera_id
join deerid.gate1b_predictions p on p.id=e.prediction_id left join latest_label l on l.automation_event_id=e.id;
$$;
revoke all on function public.deerid_gate1b_automation_audit(integer) from public, anon, authenticated;
grant execute on function public.deerid_gate1b_automation_audit(integer) to service_role;

create or replace function public.deerid_record_gate1b_automation_label(
  p_automation_event_id bigint, p_verdict text, p_note text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
declare previous_id bigint; new_id bigint; action_name text;
begin
  if p_verdict not in ('correct','should_have_requested_hd','incorrect_male_or_antler') or length(coalesce(p_note,'')) > 500 then raise exception 'invalid automation label'; end if;
  select action into action_name from deerid.gate1b_automation_events where id=p_automation_event_id;
  if action_name is null then raise exception 'automation event not found'; end if;
  if (action_name='auto_suppress_female' and p_verdict='incorrect_male_or_antler') or
     (action_name='auto_request_hd' and p_verdict='should_have_requested_hd') then raise exception 'verdict does not match automation action'; end if;
  select id into previous_id from deerid.gate1b_automation_labels where automation_event_id=p_automation_event_id order by created_at desc,id desc limit 1;
  insert into deerid.gate1b_automation_labels(automation_event_id,supersedes_id,verdict,note)
  values(p_automation_event_id,previous_id,p_verdict,nullif(trim(coalesce(p_note,'')),'')) returning id into new_id;
  if p_verdict='should_have_requested_hd' then
   insert into deerid.hd_requests(media_id, gate1_assessment_id, priority, priority_reason)
   select e.media_id,e.gate1_assessment_id,100,'gate1b_audit_correction'
   from deerid.gate1b_automation_events e where e.id=p_automation_event_id
   on conflict(media_id) do nothing;
  end if;
  return jsonb_build_object('ok',true,'label_id',new_id);
end $$;
revoke all on function public.deerid_record_gate1b_automation_label(bigint,text,text) from public, anon, authenticated;
grant execute on function public.deerid_record_gate1b_automation_label(bigint,text,text) to service_role;

create table deerid.hd_review_results (
  id bigint generated always as identity primary key,
  media_id uuid not null references deerid.media(id) on delete restrict,
  media_asset_id uuid not null references deerid.media_assets(id) on delete restrict,
  model_name text not null, model_version text not null,
  result jsonb not null, created_at timestamptz not null default now(),
  unique(media_asset_id, model_name, model_version)
);
alter table deerid.hd_review_results enable row level security;
grant select, insert on deerid.hd_review_results to service_role;
grant usage, select on sequence deerid.hd_review_results_id_seq to service_role;
revoke update, delete, truncate on deerid.hd_review_results from service_role;

create table deerid.hd_review_decisions (
  id bigint generated always as identity primary key,
  hd_review_result_id bigint not null references deerid.hd_review_results(id) on delete restrict,
  action text not null check(action in ('create_profile','match_profile','not_identity_worthy','defer')),
  animal_profile_id uuid references deerid.animal_profiles(id) on delete restrict,
  note text check(note is null or length(note)<=500), created_at timestamptz not null default now()
);
create unique index hd_review_decisions_final_once_idx on deerid.hd_review_decisions(hd_review_result_id) where action <> 'defer';
alter table deerid.hd_review_decisions enable row level security;
grant select, insert on deerid.hd_review_decisions to service_role;
grant usage, select on sequence deerid.hd_review_decisions_id_seq to service_role;
revoke update, delete, truncate on deerid.hd_review_decisions from service_role;

create or replace function deerid.reject_hd_review_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$ begin raise exception 'HD review evidence is append-only'; end $$;
create trigger hd_review_results_append_only before update or delete on deerid.hd_review_results for each row execute function deerid.reject_hd_review_mutation();
create trigger hd_review_decisions_append_only before update or delete on deerid.hd_review_decisions for each row execute function deerid.reject_hd_review_mutation();

create table deerid.hd_review_claims (
  media_asset_id uuid primary key references deerid.media_assets(id) on delete restrict,
  claim_token uuid not null unique,
  claimed_at timestamptz not null default now()
);
alter table deerid.hd_review_claims enable row level security;
grant select, insert, update, delete on deerid.hd_review_claims to service_role;

create or replace function public.deerid_claim_hd_review(p_model_name text,p_model_version text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid(); begin
  delete from deerid.hd_review_claims where claimed_at < now()-interval '30 minutes';
  select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
  and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
  and not exists(select 1 from deerid.hd_review_claims q where q.media_asset_id=a.id)
  order by a.observed_at for update skip locked limit 1;
  if chosen.id is null then return jsonb_build_object('ok',true,'empty',true); end if;
  insert into deerid.hd_review_claims(media_asset_id,claim_token) values(chosen.id,token);
  return jsonb_build_object('ok',true,'empty',false,'claim_token',token,'media_id',chosen.media_id,'media_asset_id',chosen.id,'object_path',chosen.object_path);
end $$;

create or replace function public.deerid_complete_hd_review(p_claim_token uuid,p_model_name text,p_model_version text,p_result jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; inserted_id bigint; begin
 select a.* into chosen from deerid.media_assets a join deerid.hd_review_claims q on q.media_asset_id=a.id where q.claim_token=p_claim_token for update of q;
 if chosen.id is null then raise exception 'stale HD review claim'; end if;
 insert into deerid.hd_review_results(media_id,media_asset_id,model_name,model_version,result)
 values(chosen.media_id,chosen.id,p_model_name,p_model_version,p_result)
 on conflict(media_asset_id,model_name,model_version) do nothing returning id into inserted_id;
 delete from deerid.hd_review_claims where claim_token=p_claim_token;
 return jsonb_build_object('ok',true,'inserted',inserted_id is not null);
end $$;

create or replace function public.deerid_fail_hd_review(p_claim_token uuid,p_error_category text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$ begin
 delete from deerid.hd_review_claims where claim_token=p_claim_token;
 return jsonb_build_object('ok',true,'error_category',left(coalesce(p_error_category,'failed'),80)); end $$;

create or replace function public.deerid_record_hd_review_decision(
 p_hd_review_result_id bigint, p_action text, p_profile_id uuid default null, p_display_name text default null,
 p_species text default null, p_sex text default null, p_note text default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare review_result deerid.hd_review_results%rowtype; selected_profile uuid; selected_animal uuid; captured_year integer; snapshot jsonb;
begin
 if p_action not in ('create_profile','match_profile','not_identity_worthy','defer') or length(coalesce(p_note,''))>500 then raise exception 'invalid HD review decision'; end if;
 if p_action <> 'defer' and exists(select 1 from deerid.hd_review_decisions where hd_review_result_id=p_hd_review_result_id and action<>'defer') then
   select animal_profile_id into selected_profile from deerid.hd_review_decisions where hd_review_result_id=p_hd_review_result_id and action<>'defer' limit 1;
   return jsonb_build_object('ok',true,'replayed',true,'profile_id',selected_profile);
 end if;
 select * into review_result from deerid.hd_review_results where id=p_hd_review_result_id;
 if review_result.id is null then raise exception 'HD review result not found'; end if;
 select extract(year from captured_at)::integer into captured_year from deerid.media where id=review_result.media_id;
 if p_action='create_profile' then
   if length(trim(coalesce(p_display_name,''))) not between 1 and 80 or p_species not in ('white-tailed deer','axis deer','other deer') or p_sex not in ('male','female','unknown') then raise exception 'invalid deer profile'; end if;
   insert into deerid.animals(species,display_name,sex,notes) values(p_species,trim(p_display_name),nullif(p_sex,'unknown'),p_note) returning id into selected_animal;
   insert into deerid.animal_profiles(animal_id,season_year) values(selected_animal,captured_year) returning id into selected_profile;
 elsif p_action='match_profile' then
   select ap.id into selected_profile from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
   where ap.id=p_profile_id and ap.active and a.status='active' and ap.season_year=captured_year;
   if selected_profile is null then raise exception 'invalid profile assignment'; end if;
 end if;
 if selected_profile is not null then
   snapshot:=jsonb_build_object('match_source','human','match_confidence',1,'confirmation_status','confirmed','confirmed_by',auth.uid(),'hd_review_result_id',review_result.id);
   insert into deerid.animal_media(animal_profile_id,media_id,match_source,match_confidence,confirmation_status,confirmed_by)
   values(selected_profile,review_result.media_id,'human',1,'confirmed',auth.uid()) on conflict(animal_profile_id,media_id) do update set match_source='human',match_confidence=1,confirmation_status='confirmed',confirmed_by=auth.uid();
 end if;
 insert into deerid.hd_review_decisions(hd_review_result_id,action,animal_profile_id,note)
 values(review_result.id,p_action,selected_profile,nullif(trim(coalesce(p_note,'')),''));
 return jsonb_build_object('ok',true,'action',p_action,'profile_id',selected_profile);
end $$;
revoke all on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text) from public,anon,authenticated;
grant execute on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text) to service_role;

create or replace function public.deerid_resolve_media_asset_object(p_media_asset_id uuid)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
select jsonb_build_object('object_path',object_path,'content_type',content_type) from deerid.media_assets where id=p_media_asset_id;
$$;
revoke all on function public.deerid_resolve_media_asset_object(uuid) from public,anon,authenticated;
grant execute on function public.deerid_resolve_media_asset_object(uuid) to service_role;

create or replace function public.deerid_hd_review_queue(p_limit integer default 60)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
select coalesce(jsonb_agg(jsonb_build_object('hd_review_result_id',r.id,'media_id',r.media_id,'media_asset_id',r.media_asset_id,
 'model_name',r.model_name,'model_version',r.model_version,'result',r.result,'created_at',r.created_at,
 'captured_at',m.captured_at,'camera_name',c.name) order by r.created_at), '[]'::jsonb)
from (select * from deerid.hd_review_results r where not exists(select 1 from deerid.hd_review_decisions d where d.hd_review_result_id=r.id and d.action<>'defer') order by created_at limit greatest(1,least(coalesce(p_limit,60),120))) r
join deerid.media m on m.id=r.media_id left join deerid.cameras c on c.id=m.camera_id;
$$;

revoke all on function public.deerid_claim_hd_review(text,text) from public,anon,authenticated;
revoke all on function public.deerid_complete_hd_review(uuid,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.deerid_fail_hd_review(uuid,text) from public,anon,authenticated;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
grant execute on function public.deerid_claim_hd_review(text,text) to service_role;
grant execute on function public.deerid_complete_hd_review(uuid,text,text,jsonb) to service_role;
grant execute on function public.deerid_fail_hd_review(uuid,text) to service_role;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;
