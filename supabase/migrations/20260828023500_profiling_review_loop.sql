-- Complete the returned-HD profiling review loop with durable workflow state.

create table deerid.hd_instance_review_events (
  id bigint generated always as identity primary key,
  hd_animal_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  action text not null check (action in ('defer','reopen','detector_error')),
  reason text check (reason is null or reason in ('wrong_deer','box_clipped','multiple_deer','missed_deer','inseparable','false_detection','other')),
  note text check (note is null or length(note)<=500),
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);
create index hd_instance_review_events_latest_idx on deerid.hd_instance_review_events(hd_animal_instance_id,created_at desc,id desc);
alter table deerid.hd_instance_review_events enable row level security;
revoke all on deerid.hd_instance_review_events from public,anon,authenticated,service_role;

create or replace function deerid.reject_hd_instance_review_event_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin raise exception 'HD instance review events are append-only'; end $$;
create trigger hd_instance_review_events_append_only before update or delete on deerid.hd_instance_review_events
for each row execute function deerid.reject_hd_instance_review_event_mutation();
revoke all on function deerid.reject_hd_instance_review_event_mutation() from public,anon,authenticated;

create or replace function public.deerid_record_hd_review_workflow_action(
  p_hd_review_result_id bigint,
  p_hd_animal_instance_id uuid,
  p_action text,
  p_reason text default null,
  p_note text default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare selected_instance deerid.hd_animal_instances%rowtype; event_id bigint; latest_action text; latest_reason text; latest_note text;
begin
  if p_action not in ('defer','reopen','detector_error')
     or length(coalesce(p_note,''))>500
     or (p_reason is not null and p_reason not in ('wrong_deer','box_clipped','multiple_deer','missed_deer','inseparable','false_detection','other'))
     or (p_action='detector_error' and p_reason is null)
  then raise exception 'invalid profiling workflow action'; end if;
  select * into selected_instance from deerid.hd_animal_instances
  where id=p_hd_animal_instance_id and hd_review_result_id=p_hd_review_result_id for update;
  if selected_instance.id is null then raise exception 'animal instance not found'; end if;
  if exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=selected_instance.id and d.action<>'defer') then raise exception 'animal instance already resolved'; end if;
  if exists(select 1 from deerid.hd_profile_assignment_proposals p join lateral (select action from deerid.hd_profile_assignment_proposal_events e where e.proposal_id=p.id order by e.created_at desc,e.id desc limit 1) state on state.action='pending' where p.hd_animal_instance_id=selected_instance.id) then raise exception 'animal instance has pending assignment'; end if;
  select action,reason,note into latest_action,latest_reason,latest_note from deerid.hd_instance_review_events where hd_animal_instance_id=selected_instance.id order by created_at desc,id desc limit 1;
  if latest_action=p_action and latest_reason is not distinct from p_reason and latest_note is not distinct from nullif(trim(coalesce(p_note,'')),'') then return jsonb_build_object('ok',true,'state',p_action,'replayed',true,'hd_animal_instance_id',selected_instance.id); end if;
  if p_action='reopen' and latest_action not in ('defer','detector_error') then raise exception 'profiling item is not deferred'; end if;
  insert into deerid.hd_instance_review_events(hd_animal_instance_id,action,reason,note,actor_id)
  values(selected_instance.id,p_action,p_reason,nullif(trim(coalesce(p_note,'')),''),auth.uid()) returning id into event_id;
  return jsonb_build_object('ok',true,'state',p_action,'event_id',event_id,'hd_animal_instance_id',selected_instance.id);
end $$;
revoke all on function public.deerid_record_hd_review_workflow_action(bigint,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.deerid_record_hd_review_workflow_action(bigint,uuid,text,text,text) to service_role;

create table deerid.hd_instance_geometry_events (
  id bigint generated always as identity primary key,
  hd_animal_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  supersedes_event_id bigint references deerid.hd_instance_geometry_events(id) on delete restrict,
  bbox_x double precision not null check (bbox_x between 0 and 1),
  bbox_y double precision not null check (bbox_y between 0 and 1),
  bbox_width double precision not null check (bbox_width>0 and bbox_width<=1),
  bbox_height double precision not null check (bbox_height>0 and bbox_height<=1),
  reason text not null check (reason in ('rebox','clipped_antlers','wrong_deer','other')),
  note text check (note is null or length(note)<=500),
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique(supersedes_event_id),
  check (bbox_x + bbox_width <= 1),
  check (bbox_y + bbox_height <= 1)
);
create index hd_instance_geometry_events_latest_idx on deerid.hd_instance_geometry_events(hd_animal_instance_id,created_at desc,id desc);
alter table deerid.hd_instance_geometry_events enable row level security;
revoke all on deerid.hd_instance_geometry_events from public,anon,authenticated,service_role;
create or replace function deerid.reject_hd_instance_geometry_event_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$ begin raise exception 'HD instance geometry events are append-only'; end $$;
create trigger hd_instance_geometry_events_append_only before update or delete on deerid.hd_instance_geometry_events for each row execute function deerid.reject_hd_instance_geometry_event_mutation();
revoke all on function deerid.reject_hd_instance_geometry_event_mutation() from public,anon,authenticated;

create or replace function public.deerid_correct_hd_instance_bbox(
  p_hd_review_result_id bigint,p_hd_animal_instance_id uuid,p_expected_geometry_event_id bigint,
  p_bbox_x double precision,p_bbox_y double precision,p_bbox_width double precision,p_bbox_height double precision,
  p_reason text,p_note text default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare selected_instance deerid.hd_animal_instances%rowtype; latest_event_id bigint; event_id bigint;
begin
  select * into selected_instance from deerid.hd_animal_instances where id=p_hd_animal_instance_id and hd_review_result_id=p_hd_review_result_id for update;
  if selected_instance.id is null or p_reason not in ('rebox','clipped_antlers','wrong_deer','other') or length(coalesce(p_note,''))>500 then raise exception 'invalid geometry correction'; end if;
  if p_bbox_x<0 or p_bbox_y<0 or p_bbox_width<=0 or p_bbox_height<=0 or p_bbox_x+p_bbox_width>1 or p_bbox_y+p_bbox_height>1 then raise exception 'invalid geometry correction'; end if;
  if exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=selected_instance.id and d.action<>'defer') then raise exception 'resolved geometry cannot be changed'; end if;
  if exists(select 1 from deerid.hd_profile_assignment_proposals p join lateral (select action from deerid.hd_profile_assignment_proposal_events e where e.proposal_id=p.id order by e.created_at desc,e.id desc limit 1) state on state.action='pending' where p.hd_animal_instance_id=selected_instance.id) then raise exception 'pending assignment geometry cannot be changed'; end if;
  select id into latest_event_id from deerid.hd_instance_geometry_events where hd_animal_instance_id=selected_instance.id order by created_at desc,id desc limit 1;
  if latest_event_id is distinct from p_expected_geometry_event_id then raise exception 'stale geometry correction'; end if;
  insert into deerid.hd_instance_geometry_events(hd_animal_instance_id,supersedes_event_id,bbox_x,bbox_y,bbox_width,bbox_height,reason,note,actor_id)
  values(selected_instance.id,latest_event_id,p_bbox_x,p_bbox_y,p_bbox_width,p_bbox_height,p_reason,nullif(trim(coalesce(p_note,'')),''),auth.uid()) returning id into event_id;
  insert into deerid.hd_instance_review_events(hd_animal_instance_id,action,note,actor_id) values(selected_instance.id,'reopen','geometry corrected',auth.uid());
  return jsonb_build_object('ok',true,'geometry_event_id',event_id,'hd_animal_instance_id',selected_instance.id,'bbox',jsonb_build_object('x',p_bbox_x,'y',p_bbox_y,'width',p_bbox_width,'height',p_bbox_height));
end $$;
revoke all on function public.deerid_correct_hd_instance_bbox(bigint,uuid,bigint,double precision,double precision,double precision,double precision,text,text) from public,anon,authenticated;
grant execute on function public.deerid_correct_hd_instance_bbox(bigint,uuid,bigint,double precision,double precision,double precision,double precision,text,text) to service_role;

create table deerid.hd_profile_assignment_proposals (
  id uuid primary key default gen_random_uuid(),
  hd_review_result_id bigint not null references deerid.hd_review_results(id) on delete restrict,
  hd_animal_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  proposal_action text not null check (proposal_action in ('create_profile','match_profile')),
  proposed_profile_id uuid references deerid.animal_profiles(id) on delete restrict,
  proposed_display_name text check (proposed_display_name is null or length(proposed_display_name) between 1 and 80),
  proposed_species text check (proposed_species is null or proposed_species in ('white-tailed deer','axis deer','other deer')),
  proposed_sex text check (proposed_sex is null or proposed_sex in ('male','female','unknown')),
  note text check (note is null or length(note)<=500),
  proposed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  check ((proposal_action='match_profile' and proposed_profile_id is not null) or (proposal_action='create_profile' and proposed_profile_id is null and proposed_display_name is not null and proposed_species is not null and proposed_sex is not null))
);
create index hd_profile_assignment_proposals_instance_idx on deerid.hd_profile_assignment_proposals(hd_animal_instance_id,created_at desc);
alter table deerid.hd_profile_assignment_proposals enable row level security;
revoke all on deerid.hd_profile_assignment_proposals from public,anon,authenticated,service_role;

create table deerid.hd_profile_assignment_proposal_events (
  id bigint generated always as identity primary key,
  proposal_id uuid not null references deerid.hd_profile_assignment_proposals(id) on delete restrict,
  action text not null check (action in ('pending','confirmed','undone')),
  confirmed_profile_id uuid references deerid.animal_profiles(id) on delete restrict,
  prior_animal_media_snapshot jsonb,
  resulting_animal_media_snapshot jsonb,
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);
create index hd_profile_assignment_proposal_events_latest_idx on deerid.hd_profile_assignment_proposal_events(proposal_id,created_at desc,id desc);
alter table deerid.hd_profile_assignment_proposal_events enable row level security;
revoke all on deerid.hd_profile_assignment_proposal_events from public,anon,authenticated,service_role;

create or replace function deerid.reject_hd_profile_assignment_proposal_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin raise exception 'HD profile assignment proposals are append-only'; end $$;
create trigger hd_profile_assignment_proposals_append_only before update or delete on deerid.hd_profile_assignment_proposals for each row execute function deerid.reject_hd_profile_assignment_proposal_mutation();
create trigger hd_profile_assignment_proposal_events_append_only before update or delete on deerid.hd_profile_assignment_proposal_events for each row execute function deerid.reject_hd_profile_assignment_proposal_mutation();
revoke all on function deerid.reject_hd_profile_assignment_proposal_mutation() from public,anon,authenticated;

create or replace function public.deerid_propose_hd_profile_assignment(
  p_hd_review_result_id bigint,p_hd_animal_instance_id uuid,p_action text,p_profile_id uuid default null,
  p_display_name text default null,p_species text default null,p_sex text default null,p_note text default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare selected_instance deerid.hd_animal_instances%rowtype; proposal_id uuid; captured_year integer; existing_state text; existing_action text; existing_profile_id uuid; existing_display_name text; existing_species text; existing_sex text;
begin
  select * into selected_instance from deerid.hd_animal_instances where id=p_hd_animal_instance_id and hd_review_result_id=p_hd_review_result_id for update;
  if selected_instance.id is null or p_action not in ('create_profile','match_profile') or length(coalesce(p_note,''))>500 then raise exception 'invalid assignment proposal'; end if;
  if exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=selected_instance.id and d.action<>'defer') then raise exception 'animal instance already resolved'; end if;
  select p.id,p.proposal_action,p.proposed_profile_id,p.proposed_display_name,p.proposed_species,p.proposed_sex,e.action into proposal_id,existing_action,existing_profile_id,existing_display_name,existing_species,existing_sex,existing_state from deerid.hd_profile_assignment_proposals p join lateral (
    select action from deerid.hd_profile_assignment_proposal_events pe where pe.proposal_id=p.id order by pe.created_at desc,pe.id desc limit 1
  ) e on true where p.hd_animal_instance_id=selected_instance.id order by p.created_at desc limit 1;
  if existing_state='pending' then
    if existing_action=p_action and existing_profile_id is not distinct from p_profile_id and existing_display_name is not distinct from nullif(trim(coalesce(p_display_name,'')),'') and existing_species is not distinct from p_species and existing_sex is not distinct from p_sex then
      return jsonb_build_object('ok',true,'pending_confirmation',true,'proposal_id',proposal_id,'replayed',true);
    end if;
    raise exception 'conflicting pending assignment proposal';
  end if;
  select extract(year from captured_at)::integer into captured_year from deerid.media where id=selected_instance.media_id;
  if p_action='match_profile' and not exists(select 1 from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id where ap.id=p_profile_id and ap.active and a.status='active' and ap.season_year=captured_year) then raise exception 'invalid profile assignment'; end if;
  if p_action='create_profile' and (length(trim(coalesce(p_display_name,''))) not between 1 and 80 or p_species not in ('white-tailed deer','axis deer','other deer') or p_sex not in ('male','female','unknown')) then raise exception 'invalid deer profile'; end if;
  insert into deerid.hd_profile_assignment_proposals(hd_review_result_id,hd_animal_instance_id,proposal_action,proposed_profile_id,proposed_display_name,proposed_species,proposed_sex,note,proposed_by)
  values(selected_instance.hd_review_result_id,selected_instance.id,p_action,p_profile_id,nullif(trim(coalesce(p_display_name,'')),''),p_species,p_sex,nullif(trim(coalesce(p_note,'')),''),auth.uid()) returning id into proposal_id;
  insert into deerid.hd_profile_assignment_proposal_events(proposal_id,action,actor_id) values(proposal_id,'pending',auth.uid());
  return jsonb_build_object('ok',true,'pending_confirmation',true,'proposal_id',proposal_id,'hd_animal_instance_id',selected_instance.id);
end $$;

create or replace function public.deerid_confirm_hd_profile_assignment(p_proposal_id uuid,p_expected_event_id bigint)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare proposal deerid.hd_profile_assignment_proposals%rowtype; selected_instance deerid.hd_animal_instances%rowtype; latest_state text; latest_event_id bigint; selected_profile uuid; selected_animal uuid; captured_year integer; decision_id bigint; assignment_event_id bigint; prior_snapshot jsonb; resulting_snapshot jsonb;
begin
  if p_expected_event_id is null then raise exception 'missing assignment proposal revision'; end if;
  select * into proposal from deerid.hd_profile_assignment_proposals where id=p_proposal_id for update;
  if proposal.id is null then raise exception 'proposal not found'; end if;
  select * into selected_instance from deerid.hd_animal_instances where id=proposal.hd_animal_instance_id for update;
  select id,action into latest_event_id,latest_state from deerid.hd_profile_assignment_proposal_events where proposal_id=proposal.id order by created_at desc,id desc limit 1;
  if latest_state='confirmed' and exists(select 1 from deerid.hd_profile_assignment_proposal_events where proposal_id=proposal.id and id=p_expected_event_id and action='pending') then return jsonb_build_object('ok',true,'confirmed',true,'replayed',true); end if;
  if latest_event_id is distinct from p_expected_event_id then raise exception 'stale assignment proposal'; end if;
  if latest_state<>'pending' then raise exception 'proposal is not pending'; end if;
  if exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=selected_instance.id and d.action<>'defer') then raise exception 'animal instance already resolved'; end if;
  select extract(year from captured_at)::integer into captured_year from deerid.media where id=selected_instance.media_id;
  if proposal.proposal_action='create_profile' then
    insert into deerid.animals(species,display_name,sex,notes) values(proposal.proposed_species,proposal.proposed_display_name,nullif(proposal.proposed_sex,'unknown'),proposal.note) returning id into selected_animal;
    insert into deerid.animal_profiles(animal_id,season_year) values(selected_animal,captured_year) returning id into selected_profile;
  else
    select ap.id into selected_profile from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id where ap.id=proposal.proposed_profile_id and ap.active and a.status='active' and ap.season_year=captured_year;
    if selected_profile is null then raise exception 'invalid profile assignment'; end if;
  end if;
  insert into deerid.hd_review_decisions(hd_review_result_id,hd_animal_instance_id,action,animal_profile_id,note)
  values(selected_instance.hd_review_result_id,selected_instance.id,proposal.proposal_action,selected_profile,proposal.note) returning id into decision_id;
  select to_jsonb(am) into prior_snapshot from deerid.animal_media am where am.animal_profile_id=selected_profile and am.media_id=selected_instance.media_id;
  insert into deerid.animal_media(animal_profile_id,media_id,match_source,match_confidence,confirmation_status,confirmed_by)
  values(selected_profile,selected_instance.media_id,'human',1,'confirmed',auth.uid()) on conflict(animal_profile_id,media_id) do update set match_source='human',match_confidence=1,confirmation_status='confirmed',confirmed_by=auth.uid();
  select to_jsonb(am) into resulting_snapshot from deerid.animal_media am where am.animal_profile_id=selected_profile and am.media_id=selected_instance.media_id;
  insert into deerid.hd_instance_profile_assignments(hd_animal_instance_id,animal_profile_id,hd_review_decision_id,actor_id) values(selected_instance.id,selected_profile,decision_id,auth.uid());
  insert into deerid.hd_instance_profile_assignment_events(hd_animal_instance_id,animal_profile_id,action,actor_id) values(selected_instance.id,selected_profile,'assign',auth.uid()) returning id into assignment_event_id;
  insert into deerid.hd_profile_assignment_proposal_events(proposal_id,action,confirmed_profile_id,prior_animal_media_snapshot,resulting_animal_media_snapshot,actor_id) values(proposal.id,'confirmed',selected_profile,prior_snapshot,resulting_snapshot,auth.uid());
  return jsonb_build_object('ok',true,'confirmed',true,'proposal_id',proposal.id,'profile_id',selected_profile,'assignment_event_id',assignment_event_id);
end $$;

create or replace function public.deerid_undo_hd_profile_assignment(p_proposal_id uuid,p_expected_event_id bigint)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare proposal deerid.hd_profile_assignment_proposals%rowtype; selected_instance deerid.hd_animal_instances%rowtype; latest_state text; latest_event_id bigint;
begin
  if p_expected_event_id is null then raise exception 'missing assignment proposal revision'; end if;
  select * into proposal from deerid.hd_profile_assignment_proposals where id=p_proposal_id for update;
  if proposal.id is null then raise exception 'proposal not found'; end if;
  select * into selected_instance from deerid.hd_animal_instances where id=proposal.hd_animal_instance_id for update;
  select id,action into latest_event_id,latest_state from deerid.hd_profile_assignment_proposal_events where proposal_id=proposal.id order by created_at desc,id desc limit 1;
  if latest_state='undone' and exists(select 1 from deerid.hd_profile_assignment_proposal_events where proposal_id=proposal.id and id=p_expected_event_id and action='pending') then return jsonb_build_object('ok',true,'undone',true,'replayed',true); end if;
  if latest_event_id is distinct from p_expected_event_id then raise exception 'stale assignment proposal'; end if;
  if latest_state<>'pending' then raise exception 'proposal is not pending'; end if;
  insert into deerid.hd_profile_assignment_proposal_events(proposal_id,action,actor_id) values(proposal.id,'undone',auth.uid());
  insert into deerid.hd_instance_review_events(hd_animal_instance_id,action,note,actor_id) values(proposal.hd_animal_instance_id,'reopen','assignment proposal undone',auth.uid());
  return jsonb_build_object('ok',true,'undone',true,'proposal_id',proposal.id,'hd_animal_instance_id',proposal.hd_animal_instance_id);
end $$;

revoke all on function public.deerid_propose_hd_profile_assignment(bigint,uuid,text,uuid,text,text,text,text) from public,anon,authenticated;
revoke all on function public.deerid_confirm_hd_profile_assignment(uuid,bigint) from public,anon,authenticated;
revoke all on function public.deerid_undo_hd_profile_assignment(uuid,bigint) from public,anon,authenticated;
grant execute on function public.deerid_propose_hd_profile_assignment(bigint,uuid,text,uuid,text,text,text,text) to service_role;
grant execute on function public.deerid_confirm_hd_profile_assignment(uuid,bigint) to service_role;
grant execute on function public.deerid_undo_hd_profile_assignment(uuid,bigint) to service_role;

create or replace function public.deerid_hd_review_queue_page(
  p_limit integer default 15,
  p_camera_id uuid default null,
  p_queue text default 'active'
) returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
  select distinct on (r.media_asset_id) r.*
  from deerid.hd_review_results r
  order by r.media_asset_id,r.created_at desc,r.id desc
), candidates as (
  select i.*,r.model_name,r.model_version,r.created_at result_created_at,m.captured_at,m.camera_id,c.name camera_name,
    (select count(*)::integer from deerid.hd_animal_instances sibling where sibling.hd_review_result_id=i.hd_review_result_id) instance_count,
    workflow.action workflow_action,workflow.reason workflow_reason,workflow.note workflow_note,
    geometry.geometry_event_id,coalesce(geometry.bbox_x,i.bbox_x) active_bbox_x,coalesce(geometry.bbox_y,i.bbox_y) active_bbox_y,coalesce(geometry.bbox_width,i.bbox_width) active_bbox_width,coalesce(geometry.bbox_height,i.bbox_height) active_bbox_height,geometry_history.items geometry_history,
    proposal.proposal_id,proposal.proposal_action,proposal.proposed_profile_id,proposal.proposed_display_name,proposal.proposal_event_id,proposal.proposal_state
  from latest_results r
  join deerid.hd_animal_instances i on i.hd_review_result_id=r.id
  join deerid.media m on m.id=i.media_id
  left join deerid.cameras c on c.id=m.camera_id
  left join lateral (
    select e.action,e.reason,e.note from deerid.hd_instance_review_events e
    where e.hd_animal_instance_id=i.id order by e.created_at desc,e.id desc limit 1
  ) workflow on true
  left join lateral (
    select e.id geometry_event_id,e.bbox_x,e.bbox_y,e.bbox_width,e.bbox_height from deerid.hd_instance_geometry_events e
    where e.hd_animal_instance_id=i.id order by e.created_at desc,e.id desc limit 1
  ) geometry on true
  left join lateral (
    select coalesce(jsonb_agg(jsonb_build_object('id',h.id,'reason',h.reason,'note',h.note,'created_at',h.created_at,'bbox',jsonb_build_object('x',h.bbox_x,'y',h.bbox_y,'width',h.bbox_width,'height',h.bbox_height)) order by h.created_at desc,h.id desc),'[]'::jsonb) items
    from (select * from deerid.hd_instance_geometry_events e where e.hd_animal_instance_id=i.id order by e.created_at desc,e.id desc limit 10) h
  ) geometry_history on true
  left join lateral (
    select p.id proposal_id,p.proposal_action,p.proposed_profile_id,p.proposed_display_name,pe.id proposal_event_id,pe.action proposal_state
    from deerid.hd_profile_assignment_proposals p
    join lateral (
      select id,action from deerid.hd_profile_assignment_proposal_events e where e.proposal_id=p.id order by e.created_at desc,e.id desc limit 1
    ) pe on true
    where p.hd_animal_instance_id=i.id order by p.created_at desc limit 1
  ) proposal on true
  where (p_camera_id is null or m.camera_id=p_camera_id)
  and not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
  and not exists(
    select 1 from deerid.hd_instance_profile_assignment_events e
    where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null
    and not exists(select 1 from deerid.hd_instance_profile_assignment_events later where later.supersedes_event_id=e.id)
  )
), filtered as (
  select * from candidates
  where (p_queue='active' and coalesce(workflow_action,'reopen')='reopen' and coalesce(proposal_state,'none')<>'pending')
     or (p_queue='deferred' and workflow_action='defer' and coalesce(proposal_state,'none')<>'pending')
     or (p_queue='pending' and proposal_state='pending')
     or (p_queue='issues' and workflow_action='detector_error' and coalesce(proposal_state,'none')<>'pending')
  order by result_created_at,instance_index
  limit greatest(1,least(coalesce(p_limit,15),31))
), visible as (
  select * from filtered limit greatest(1,least(coalesce(p_limit,15),31))-1
)
select jsonb_build_object(
  'items',coalesce((select jsonb_agg(jsonb_build_object(
    'hd_review_result_id',v.hd_review_result_id,'hd_animal_instance_id',v.id,
    'media_id',v.media_id,'media_asset_id',v.media_asset_id,
    'instance_index',v.instance_index,'instance_count',v.instance_count,
    'bbox',jsonb_build_object('x',v.active_bbox_x,'y',v.active_bbox_y,'width',v.active_bbox_width,'height',v.active_bbox_height),
    'original_bbox',jsonb_build_object('x',v.bbox_x,'y',v.bbox_y,'width',v.bbox_width,'height',v.bbox_height),'geometry_event_id',v.geometry_event_id,'geometry_history',v.geometry_history,
    'detection_complete',v.detection_complete,'detection_notes',v.detection_notes,
    'model_name',v.model_name,'model_version',v.model_version,'result',v.analysis,
    'created_at',v.result_created_at,'captured_at',v.captured_at,
    'camera_id',v.camera_id,'camera_name',v.camera_name,'workflow_state',coalesce(v.workflow_action,'active'),'workflow_reason',v.workflow_reason,'workflow_note',v.workflow_note,
    'proposal_id',v.proposal_id,'proposal_event_id',v.proposal_event_id,'proposal_action',v.proposal_action,'proposed_profile_id',v.proposed_profile_id,'proposed_display_name',v.proposed_display_name,'proposal_state',v.proposal_state
  ) order by v.result_created_at,v.instance_index) from visible v),'[]'::jsonb),
  'has_more',(select count(*) from filtered)>greatest(1,least(coalesce(p_limit,15),31))-1,
  'progress',public.deerid_hd_review_progress()
);
$$;
revoke all on function public.deerid_hd_review_queue_page(integer,uuid,text) from public,anon,authenticated;
grant execute on function public.deerid_hd_review_queue_page(integer,uuid,text) to service_role;

create or replace function public.deerid_hd_review_progress()
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
 select distinct on (r.media_asset_id) r.* from deerid.hd_review_results r order by r.media_asset_id,r.created_at desc,r.id desc
), base as (
 select i.id,i.created_at,m.camera_id,
   exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
   or exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null and not exists(select 1 from deerid.hd_instance_profile_assignment_events later where later.supersedes_event_id=e.id)) resolved,
   workflow.action workflow_action,proposal.proposal_state
 from latest_results r join deerid.hd_animal_instances i on i.hd_review_result_id=r.id join deerid.media m on m.id=i.media_id
 left join lateral (select action from deerid.hd_instance_review_events e where e.hd_animal_instance_id=i.id order by e.created_at desc,e.id desc limit 1) workflow on true
 left join lateral (
   select state.action proposal_state from deerid.hd_profile_assignment_proposals p join lateral (select action from deerid.hd_profile_assignment_proposal_events pe where pe.proposal_id=p.id order by pe.created_at desc,pe.id desc limit 1) state on true
   where p.hd_animal_instance_id=i.id order by p.created_at desc limit 1
 ) proposal on true
), unresolved as (
 select *,case when proposal_state='pending' then 'pending_confirmation' when workflow_action='defer' then 'deferred' when workflow_action='detector_error' then 'detector_error' else 'active' end queue_state from base where not resolved
)
select jsonb_build_object(
 'total',(select count(*) from base),
 'completed',(select count(*) from base where resolved),
 'remaining',(select count(*) from unresolved),
 'profiling_ready',(select count(*) from unresolved where queue_state='active'),
 'deferred',(select count(*) from unresolved where queue_state='deferred'),
 'pending_confirmation',(select count(*) from unresolved where queue_state='pending_confirmation'),
 'detector_errors',(select count(*) from unresolved where queue_state='detector_error'),
 'by_camera',coalesce((select jsonb_object_agg(camera_id,cnt) from (select camera_id,count(*) cnt from unresolved where camera_id is not null group by camera_id) grouped),'{}'::jsonb)
);
$$;
revoke all on function public.deerid_hd_review_progress() from public,anon,authenticated;
grant execute on function public.deerid_hd_review_progress() to service_role;

create or replace function public.deerid_pipeline_health()
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
select jsonb_build_object(
 'as_of',now(),
 'overall',case when
   exists(select 1 from deerid.ingestion_runs where status in ('failed','degraded') and finished_at>=now()-interval '24 hours')
   or exists(select 1 from deerid.gate1_claims where leased_until<=now())
   or exists(select 1 from deerid.gate1b_claims where claimed_at<now()-interval '10 minutes')
   or exists(select 1 from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes')
   or exists(select 1 from deerid.hd_requests where status in ('queued','requesting') and created_at<now()-interval '30 minutes')
   or exists(select 1 from deerid.hd_requests r where r.status='submitted' and r.submitted_at<now()-interval '24 hours' and not exists(select 1 from deerid.media_assets a where a.media_id=r.media_id and a.variant='cloud_hd'))
   or exists(select 1 from deerid.hd_requests where status in ('failed','unknown') and updated_at>=now()-interval '24 hours')
   or exists(select 1 from deerid.hd_review_failures where created_at>=now()-interval '24 hours')
   then 'degraded' else 'unknown' end,
 'stages',jsonb_build_object(
  'ingestion',jsonb_build_object(
    'last_success_at',(select max(finished_at) from deerid.ingestion_runs where status='succeeded'),
    'pending_count',null,'oldest_pending_at',null,'stale_claim_count',null,
    'failure_count_24h',(select count(*) from deerid.ingestion_runs where status in ('failed','degraded') and finished_at>=now()-interval '24 hours'),
    'telemetry_complete',true),
  'gate1',jsonb_build_object(
    'last_success_at',(select max(created_at) from deerid.gate1_assessments where model_name='SpeciesNet' and model_version='4.0.3a'),
    'pending_count',(select count(*) from deerid.media m where m.variant='cloud_thumbnail' and not exists(select 1 from deerid.gate1_assessments a where a.media_id=m.id and a.model_name='SpeciesNet' and a.model_version='4.0.3a')),
    'oldest_pending_at',(select min(m.captured_at) from deerid.media m where m.variant='cloud_thumbnail' and not exists(select 1 from deerid.gate1_assessments a where a.media_id=m.id and a.model_name='SpeciesNet' and a.model_version='4.0.3a')),
    'stale_claim_count',(select count(*) from deerid.gate1_claims where leased_until<=now()),
    'failure_count_24h',null,'telemetry_complete',false),
  'gate1b',jsonb_build_object(
    'last_success_at',(select max(created_at) from deerid.gate1b_predictions where model_name='OpenAI-GPT-4o-mini-Vision' and model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'),
    'pending_count',(select count(*) from (select distinct on (a.media_id) a.* from deerid.gate1_assessments a where a.model_name='SpeciesNet' and a.model_version='4.0.3a' order by a.media_id,a.created_at desc,a.id desc) a join deerid.media m on m.id=a.media_id where m.variant='cloud_thumbnail' and a.route in ('review','archive') and (a.is_representative or a.route='review') and not exists(select 1 from deerid.gate1b_predictions p where p.gate1_assessment_id=a.id and p.model_name='OpenAI-GPT-4o-mini-Vision' and p.model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1')),
    'oldest_pending_at',(select min(a.created_at) from (select distinct on (a.media_id) a.* from deerid.gate1_assessments a where a.model_name='SpeciesNet' and a.model_version='4.0.3a' order by a.media_id,a.created_at desc,a.id desc) a join deerid.media m on m.id=a.media_id where m.variant='cloud_thumbnail' and a.route in ('review','archive') and (a.is_representative or a.route='review') and not exists(select 1 from deerid.gate1b_predictions p where p.gate1_assessment_id=a.id and p.model_name='OpenAI-GPT-4o-mini-Vision' and p.model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1')),
    'stale_claim_count',(select count(*) from deerid.gate1b_claims where claimed_at<now()-interval '10 minutes'),
    'failure_count_24h',null,'telemetry_complete',false),
  'hd_requests',jsonb_build_object(
    'last_success_at',(select max(submitted_at) from deerid.hd_requests where status in ('submitted','available')),
    'pending_count',(select count(*) from deerid.hd_requests where status in ('queued','requesting')),
    'oldest_pending_at',(select min(created_at) from deerid.hd_requests where status in ('queued','requesting')),
    'stale_claim_count',(select count(*) from deerid.hd_requests where status='requesting' and request_started_at<now()-interval '15 minutes'),
    'failure_count_24h',(select count(*) from deerid.hd_requests where status in ('failed','unknown') and updated_at>=now()-interval '24 hours'),
    'telemetry_complete',true),
  'hd_returns',jsonb_build_object(
    'last_success_at',(select max(observed_at) from deerid.media_assets where variant='cloud_hd'),
    'pending_count',(select count(*) from deerid.hd_requests r where r.status='submitted' and not exists(select 1 from deerid.media_assets a where a.media_id=r.media_id and a.variant='cloud_hd')),
    'oldest_pending_at',(select min(r.submitted_at) from deerid.hd_requests r where r.status='submitted' and not exists(select 1 from deerid.media_assets a where a.media_id=r.media_id and a.variant='cloud_hd')),
    'stale_claim_count',null,'failure_count_24h',null,'telemetry_complete',false),
  'hd_analysis',jsonb_build_object(
    'last_success_at',(select max(created_at) from deerid.hd_review_results),
    'pending_count',(select count(*) from deerid.media_assets a where a.variant='cloud_hd' and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id) and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)),
    'oldest_pending_at',(select min(a.observed_at) from deerid.media_assets a where a.variant='cloud_hd' and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id) and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)),
    'stale_claim_count',(select count(*) from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes'),
    'failure_count_24h',(select count(*) from deerid.hd_review_failures where created_at>=now()-interval '24 hours'),
    'telemetry_complete',true),
  'profiling',jsonb_build_object(
    'last_success_at',(select max(created_at) from deerid.hd_review_decisions where action<>'defer'),
    'pending_count',(select count(*) from deerid.hd_animal_instances i join deerid.hd_review_results r on r.id=i.hd_review_result_id where not exists(select 1 from deerid.hd_review_results newer where newer.media_asset_id=r.media_asset_id and (newer.created_at,newer.id)>(r.created_at,r.id)) and not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer') and not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null)),
    'oldest_pending_at',(select min(i.created_at) from deerid.hd_animal_instances i join deerid.hd_review_results r on r.id=i.hd_review_result_id where not exists(select 1 from deerid.hd_review_results newer where newer.media_asset_id=r.media_asset_id and (newer.created_at,newer.id)>(r.created_at,r.id)) and not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer') and not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null)),
    'stale_claim_count',null,'failure_count_24h',null,'telemetry_complete',false)
  )
);
$$;
revoke all on function public.deerid_pipeline_health() from public,anon,authenticated;
grant execute on function public.deerid_pipeline_health() to service_role;

-- Make corrected geometry authoritative in profile cards, galleries, representatives, and beginner comparisons.
create or replace function public.deerid_profiles() returns jsonb
language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_assignments as (
 select e.* from deerid.hd_instance_profile_assignment_events e where not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)
), current_representatives as (
 select distinct on (r.animal_profile_id) r.* from deerid.profile_representative_events r where not exists(select 1 from deerid.profile_representative_events n where n.supersedes_event_id=r.id) order by r.animal_profile_id,r.created_at desc,r.id desc
), profile_rows as (
 select ap.id,a.id animal_id,a.display_name,a.species,coalesce(a.sex,'unknown') sex,ap.season_year,counts.photo_count,counts.first_seen,counts.last_seen,counts.camera_ids,counts.camera_names,previews.items profile_previews,previews.representative_assignment_event_id
 from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
 left join lateral (
  select count(am.media_id)::int photo_count,min(m.captured_at) first_seen,max(m.captured_at) last_seen,coalesce(jsonb_agg(distinct m.camera_id) filter(where m.camera_id is not null),'[]'::jsonb) camera_ids,coalesce(jsonb_agg(distinct c.name) filter(where c.name is not null),'[]'::jsonb) camera_names
  from deerid.animal_media am join deerid.media m on m.id=am.media_id left join deerid.cameras c on c.id=m.camera_id where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
 ) counts on true
 left join lateral (
  select coalesce(jsonb_agg(jsonb_build_object('media_id',picked.media_id,'media_asset_id',picked.media_asset_id,'hd_animal_instance_id',picked.hd_animal_instance_id,'assignment_event_id',picked.assignment_event_id,'captured_at',picked.captured_at,'bbox',picked.bbox,'crop_recipe',picked.crop_recipe,'is_representative',picked.is_representative) order by picked.is_representative desc,picked.captured_at desc),'[]'::jsonb) items,max(picked.assignment_event_id) filter(where picked.is_representative) representative_assignment_event_id
  from (
   select i.media_id,i.media_asset_id,i.id hd_animal_instance_id,ce.id assignment_event_id,m.captured_at,
    jsonb_build_object('x',coalesce(g.bbox_x,i.bbox_x),'y',coalesce(g.bbox_y,i.bbox_y),'width',coalesce(g.bbox_width,i.bbox_width),'height',coalesce(g.bbox_height,i.bbox_height)) bbox,i.crop_recipe,
    (cr.assignment_event_id=ce.id and cr.animal_profile_id=ap.id)::boolean is_representative
   from current_assignments ce join deerid.hd_animal_instances i on i.id=ce.hd_animal_instance_id join deerid.media m on m.id=i.media_id
   left join lateral (select * from deerid.hd_instance_geometry_events ge where ge.hd_animal_instance_id=i.id order by ge.created_at desc,ge.id desc limit 1) g on true
   left join current_representatives cr on cr.animal_profile_id=ap.id and cr.assignment_event_id=ce.id
   where ce.animal_profile_id=ap.id order by is_representative desc,m.captured_at desc limit 5
  ) picked
 ) previews on true
 where ap.active and a.status='active'
)
select coalesce(jsonb_agg(to_jsonb(profile_rows) order by display_name),'[]'::jsonb) from profile_rows;
$$;
revoke all on function public.deerid_profiles() from public,anon,authenticated;
grant execute on function public.deerid_profiles() to service_role;

create or replace function public.deerid_profile_gallery_page(p_profile_id uuid,p_limit integer default 24)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_events as (
 select distinct on (e.hd_animal_instance_id) e.* from deerid.hd_instance_profile_assignment_events e order by e.hd_animal_instance_id,e.created_at desc,e.id desc
), picked as (
 select am.animal_profile_id,am.media_id,m.captured_at,m.camera_id,c.name camera_name,ce.id assignment_event_id,i.id hd_animal_instance_id,i.media_asset_id,
  case when i.id is null then null else jsonb_build_object('x',coalesce(g.bbox_x,i.bbox_x),'y',coalesce(g.bbox_y,i.bbox_y),'width',coalesce(g.bbox_width,i.bbox_width),'height',coalesce(g.bbox_height,i.bbox_height)) end bbox,i.crop_recipe
 from deerid.animal_media am join deerid.media m on m.id=am.media_id left join deerid.cameras c on c.id=m.camera_id
 left join lateral (
  select e.* from current_events e join deerid.hd_animal_instances candidate_i on candidate_i.id=e.hd_animal_instance_id
  where e.animal_profile_id=am.animal_profile_id and candidate_i.media_id=am.media_id order by e.created_at desc,e.id desc limit 1
 ) ce on true
 left join deerid.hd_animal_instances i on i.id=ce.hd_animal_instance_id
 left join lateral (select * from deerid.hd_instance_geometry_events ge where ge.hd_animal_instance_id=i.id order by ge.created_at desc,ge.id desc limit 1) g on true
 where am.confirmation_status='confirmed' and am.animal_profile_id=p_profile_id and i.id is not null
 order by m.captured_at desc,am.media_id limit greatest(1,least(coalesce(p_limit,24),60))
)
select coalesce(jsonb_agg(jsonb_build_object('assignment_event_id',assignment_event_id,'hd_animal_instance_id',hd_animal_instance_id,'animal_profile_id',animal_profile_id,'media_id',media_id,'media_asset_id',media_asset_id,'captured_at',captured_at,'camera_id',camera_id,'camera_name',camera_name,'bbox',bbox,'crop_recipe',crop_recipe) order by captured_at desc,media_id),'[]'::jsonb) from picked;
$$;
revoke all on function public.deerid_profile_gallery_page(uuid,integer) from public,anon,authenticated;
grant execute on function public.deerid_profile_gallery_page(uuid,integer) to service_role;
