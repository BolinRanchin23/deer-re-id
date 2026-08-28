-- Append-only human corrections to the animal-instance topology of returned HD results.
create table deerid.hd_instance_topology_events (
  id bigint generated always as identity primary key,
  request_id uuid not null unique,
  hd_review_result_id bigint not null references deerid.hd_review_results(id) on delete restrict,
  source_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  supersedes_event_id bigint references deerid.hd_instance_topology_events(id) on delete restrict,
  action text not null check (action in ('add','split','remove','inseparable')),
  boxes jsonb not null check (jsonb_typeof(boxes)='array'),
  resulting_instance_ids uuid[] not null default '{}',
  note text check (note is null or length(note)<=500),
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique(supersedes_event_id)
);
alter table deerid.hd_animal_instances
  add column origin_kind text not null default 'model' check (origin_kind in ('model','human_add','human_split')),
  add column analysis_status text not null default 'complete' check (analysis_status in ('complete','not_run')),
  add column introduced_by_topology_request_id uuid references deerid.hd_instance_topology_events(request_id) on delete restrict deferrable initially deferred,
  add column split_from_hd_animal_instance_id uuid references deerid.hd_animal_instances(id) on delete restrict,
  add constraint hd_animal_instances_origin_consistent check (
    (origin_kind='model' and introduced_by_topology_request_id is null and split_from_hd_animal_instance_id is null and analysis_status='complete')
    or (origin_kind='human_add' and introduced_by_topology_request_id is not null and split_from_hd_animal_instance_id is null and analysis_status='not_run')
    or (origin_kind='human_split' and introduced_by_topology_request_id is not null and split_from_hd_animal_instance_id is not null and analysis_status='not_run')
  );
alter table deerid.hd_animal_instances drop constraint if exists hd_animal_instances_instance_index_check;
alter table deerid.hd_animal_instances add constraint hd_animal_instances_instance_index_positive check (instance_index>=1);
create index hd_instance_topology_events_latest_idx on deerid.hd_instance_topology_events(source_instance_id,created_at desc,id desc);
alter table deerid.hd_instance_topology_events enable row level security;
revoke all on deerid.hd_instance_topology_events from public,anon,authenticated,service_role;

create or replace function deerid.reject_hd_instance_topology_event_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$ begin raise exception 'HD instance topology events are append-only'; end $$;
create trigger hd_instance_topology_events_append_only before update or delete on deerid.hd_instance_topology_events for each row execute function deerid.reject_hd_instance_topology_event_mutation();
revoke all on function deerid.reject_hd_instance_topology_event_mutation() from public,anon,authenticated;

create or replace function deerid.reject_terminal_topology_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin
  if exists(
    select 1 from deerid.hd_instance_topology_events e
    where e.source_instance_id=new.hd_animal_instance_id
    order by e.created_at desc,e.id desc limit 1
  ) and (select e.action in ('split','remove','inseparable') from deerid.hd_instance_topology_events e where e.source_instance_id=new.hd_animal_instance_id order by e.created_at desc,e.id desc limit 1)
  then raise exception 'animal instance topology is terminal'; end if;
  return new;
end $$;
create trigger hd_review_decisions_terminal_topology before insert on deerid.hd_review_decisions for each row execute function deerid.reject_terminal_topology_mutation();
create trigger hd_profile_assignment_proposals_terminal_topology before insert on deerid.hd_profile_assignment_proposals for each row execute function deerid.reject_terminal_topology_mutation();
create trigger hd_instance_geometry_events_terminal_topology before insert on deerid.hd_instance_geometry_events for each row execute function deerid.reject_terminal_topology_mutation();
create trigger hd_instance_review_events_terminal_topology before insert on deerid.hd_instance_review_events for each row execute function deerid.reject_terminal_topology_mutation();
revoke all on function deerid.reject_terminal_topology_mutation() from public,anon,authenticated;

create or replace function deerid.lock_hd_review_result_media_asset()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(new.media_asset_id::text,1));
  return new;
end $$;
create trigger hd_review_results_media_asset_lock before insert on deerid.hd_review_results for each row execute function deerid.lock_hd_review_result_media_asset();
revoke all on function deerid.lock_hd_review_result_media_asset() from public,anon,authenticated;

create or replace function public.deerid_correct_hd_instance_topology(
  p_hd_review_result_id bigint,p_source_instance_id uuid,p_expected_topology_event_id bigint,
  p_request_id uuid,p_action text,p_boxes jsonb,p_note text default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare
  source_instance deerid.hd_animal_instances%rowtype; selected_result deerid.hd_review_results%rowtype;
  existing deerid.hd_instance_topology_events%rowtype; latest_event_id bigint; latest_action text;
  box jsonb; bx double precision; by_ double precision; bw double precision; bh double precision;
  box_count integer; active_count integer; next_index integer; child_id uuid; child_ids uuid[]='{}'; event_id bigint;
begin
  if p_request_id is null or p_action not in ('add','split','remove','inseparable') or jsonb_typeof(p_boxes)<>'array' or length(coalesce(p_note,''))>500 then raise exception 'invalid topology correction'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_request_id::text,0));
  box_count=jsonb_array_length(p_boxes);
  if (p_action='add' and box_count<>1) or (p_action='split' and box_count not between 2 and 5) or (p_action in ('remove','inseparable') and box_count<>0) then raise exception 'invalid topology correction'; end if;
  select * into existing from deerid.hd_instance_topology_events where request_id=p_request_id;
  if existing.id is not null then
    if existing.hd_review_result_id is distinct from p_hd_review_result_id or existing.source_instance_id is distinct from p_source_instance_id or existing.supersedes_event_id is distinct from p_expected_topology_event_id or existing.action is distinct from p_action or existing.boxes is distinct from p_boxes or existing.note is distinct from nullif(trim(coalesce(p_note,'')),'') then raise exception 'conflicting topology request'; end if;
    return jsonb_build_object('ok',true,'action',existing.action,'topology_event_id',existing.id,'source_instance_id',existing.source_instance_id,'resulting_instance_ids',to_jsonb(existing.resulting_instance_ids),'replayed',true);
  end if;
  select * into selected_result from deerid.hd_review_results where id=p_hd_review_result_id for update;
  if selected_result.id is null then raise exception 'animal instance not found'; end if;
  perform pg_advisory_xact_lock(hashtextextended(selected_result.media_asset_id::text,1));
  select * into source_instance from deerid.hd_animal_instances where id=p_source_instance_id and hd_review_result_id=p_hd_review_result_id for update;
  if source_instance.id is null then raise exception 'animal instance not found'; end if;
  if exists(select 1 from deerid.hd_review_results newer where newer.media_asset_id=selected_result.media_asset_id and (newer.created_at,newer.id)>(selected_result.created_at,selected_result.id)) then raise exception 'stale HD review result'; end if;
  select * into existing from deerid.hd_instance_topology_events where request_id=p_request_id;
  if existing.id is not null then
    if existing.hd_review_result_id is distinct from p_hd_review_result_id or existing.source_instance_id is distinct from p_source_instance_id or existing.supersedes_event_id is distinct from p_expected_topology_event_id or existing.action is distinct from p_action or existing.boxes is distinct from p_boxes or existing.note is distinct from nullif(trim(coalesce(p_note,'')),'') then raise exception 'conflicting topology request'; end if;
    return jsonb_build_object('ok',true,'action',existing.action,'topology_event_id',existing.id,'source_instance_id',existing.source_instance_id,'resulting_instance_ids',to_jsonb(existing.resulting_instance_ids),'replayed',true);
  end if;
  if exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=source_instance.id and d.action<>'defer') then raise exception 'animal instance already resolved'; end if;
  if exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=source_instance.id and e.animal_profile_id is not null and not exists(select 1 from deerid.hd_instance_profile_assignment_events later where later.supersedes_event_id=e.id)) then raise exception 'animal instance already assigned'; end if;
  if exists(select 1 from deerid.hd_profile_assignment_proposals p join lateral (select action from deerid.hd_profile_assignment_proposal_events e where e.proposal_id=p.id order by e.created_at desc,e.id desc limit 1) state on state.action='pending' where p.hd_animal_instance_id=source_instance.id) then raise exception 'animal instance has pending assignment'; end if;
  select id,action into latest_event_id,latest_action from deerid.hd_instance_topology_events where source_instance_id=source_instance.id order by created_at desc,id desc limit 1;
  if latest_event_id is distinct from p_expected_topology_event_id then raise exception 'stale topology correction'; end if;
  if latest_action in ('split','remove','inseparable') then raise exception 'animal instance topology is terminal'; end if;
  select count(*) into active_count from deerid.hd_animal_instances i
  left join lateral (select e.action from deerid.hd_instance_topology_events e where e.source_instance_id=i.id order by e.created_at desc,e.id desc limit 1) t on true
  where i.hd_review_result_id=selected_result.id and coalesce(t.action,'add') not in ('split','remove','inseparable');
  if (p_action='add' and active_count+1>20) or (p_action='split' and active_count-1+box_count>20) then raise exception 'too many animal instances'; end if;
  for box in select value from jsonb_array_elements(p_boxes) loop
    if jsonb_typeof(box)<>'object' or not (box ?& array['x','y','width','height']) or (box - array['x','y','width','height'])<>'{}'::jsonb then raise exception 'invalid topology box'; end if;
    begin bx=(box->>'x')::double precision;by_=(box->>'y')::double precision;bw=(box->>'width')::double precision;bh=(box->>'height')::double precision; exception when others then raise exception 'invalid topology box'; end;
    if bx::text in ('NaN','Infinity','-Infinity') or by_::text in ('NaN','Infinity','-Infinity') or bw::text in ('NaN','Infinity','-Infinity') or bh::text in ('NaN','Infinity','-Infinity') or bx<0 or by_<0 or bw<=0 or bh<=0 or bx+bw>1 or by_+bh>1 then raise exception 'invalid topology box'; end if;
  end loop;
  if p_action='split' and (select count(*) from jsonb_array_elements(p_boxes))<>(select count(distinct value) from jsonb_array_elements(p_boxes)) then raise exception 'duplicate topology box'; end if;
  if box_count>0 then
    select coalesce(max(instance_index),0) into next_index from deerid.hd_animal_instances where hd_review_result_id=selected_result.id;
    for box in select value from jsonb_array_elements(p_boxes) loop
      next_index=next_index+1;bx=(box->>'x')::double precision;by_=(box->>'y')::double precision;bw=(box->>'width')::double precision;bh=(box->>'height')::double precision;
      insert into deerid.hd_animal_instances(hd_review_result_id,media_id,media_asset_id,instance_index,bbox_x,bbox_y,bbox_width,bbox_height,detection_complete,detection_notes,analysis,crop_recipe,origin_kind,analysis_status,introduced_by_topology_request_id,split_from_hd_animal_instance_id)
      values(selected_result.id,source_instance.media_id,source_instance.media_asset_id,next_index,bx,by_,bw,bh,false,case when p_action='add' then 'Human-added missed deer; model analysis not run.' else 'Human-created split crop; model analysis not run.' end,
        jsonb_build_object('species','unknown','sex','unknown','summary','Human-created crop; model description unavailable','identity_eligible',null,'age_eligible',false,'antler_score_eligible',false,'distinguishing_features','[]'::jsonb),
        jsonb_build_object('kind','normalized_bbox','source','human_topology_correction','request_id',p_request_id,'action',p_action),
        case when p_action='add' then 'human_add' else 'human_split' end,'not_run',p_request_id,case when p_action='split' then source_instance.id else null end) returning id into child_id;
      child_ids=array_append(child_ids,child_id);
    end loop;
  end if;
  insert into deerid.hd_instance_topology_events(request_id,hd_review_result_id,source_instance_id,supersedes_event_id,action,boxes,resulting_instance_ids,note,actor_id)
  values(p_request_id,selected_result.id,source_instance.id,latest_event_id,p_action,p_boxes,child_ids,nullif(trim(coalesce(p_note,'')),''),auth.uid()) returning id into event_id;
  if p_action='add' then insert into deerid.hd_instance_review_events(hd_animal_instance_id,action,note,actor_id) values(source_instance.id,'reopen','missed deer added',auth.uid()); end if;
  return jsonb_build_object('ok',true,'action',p_action,'topology_event_id',event_id,'source_instance_id',source_instance.id,'resulting_instance_ids',to_jsonb(child_ids),'replayed',false);
end $$;
revoke all on function public.deerid_correct_hd_instance_topology(bigint,uuid,bigint,uuid,text,jsonb,text) from public,anon,authenticated;
grant execute on function public.deerid_correct_hd_instance_topology(bigint,uuid,bigint,uuid,text,jsonb,text) to service_role;
revoke all on function public.deerid_hd_review_queue(integer) from service_role;

create or replace function public.deerid_hd_review_queue_page(
  p_limit integer default 15,
  p_camera_id uuid default null,
  p_queue text default 'active'
) returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
  select distinct on (r.media_asset_id) r.*
  from deerid.hd_review_results r
  order by r.media_asset_id,r.created_at desc,r.id desc
), latest_topology as (
  select distinct on (e.source_instance_id) e.source_instance_id,e.id topology_event_id,e.action topology_action
  from deerid.hd_instance_topology_events e order by e.source_instance_id,e.created_at desc,e.id desc
), topology_base as (
  select i.*,t.topology_event_id,t.topology_action from deerid.hd_animal_instances i left join latest_topology t on t.source_instance_id=i.id
), active_instances as (
  select b.*,row_number() over(partition by b.hd_review_result_id order by b.instance_index,b.id)::integer active_instance_index,
    count(*) over(partition by b.hd_review_result_id)::integer active_instance_count
  from topology_base b where b.topology_action is null or b.topology_action not in ('split','remove','inseparable')
), candidates as (
  select i.*,r.model_name,r.model_version,r.created_at result_created_at,m.captured_at,m.camera_id,c.name camera_name,
    i.active_instance_count instance_count,
    workflow.action workflow_action,workflow.reason workflow_reason,workflow.note workflow_note,
    geometry.geometry_event_id,coalesce(geometry.bbox_x,i.bbox_x) active_bbox_x,coalesce(geometry.bbox_y,i.bbox_y) active_bbox_y,coalesce(geometry.bbox_width,i.bbox_width) active_bbox_width,coalesce(geometry.bbox_height,i.bbox_height) active_bbox_height,geometry_history.items geometry_history,
    proposal.proposal_id,proposal.proposal_action,proposal.proposed_profile_id,proposal.proposed_display_name,proposal.proposal_event_id,proposal.proposal_state
  from latest_results r
  join active_instances i on i.hd_review_result_id=r.id
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
    'instance_index',v.active_instance_index,'instance_count',v.instance_count,
    'bbox',jsonb_build_object('x',v.active_bbox_x,'y',v.active_bbox_y,'width',v.active_bbox_width,'height',v.active_bbox_height),
    'original_bbox',jsonb_build_object('x',v.bbox_x,'y',v.bbox_y,'width',v.bbox_width,'height',v.bbox_height),'geometry_event_id',v.geometry_event_id,'geometry_history',v.geometry_history,
    'detection_complete',v.detection_complete,'detection_notes',v.detection_notes,'review_origin',case when v.origin_kind='model' then 'model_detection' else 'human_topology_correction' end,'analysis_status',case when v.analysis_status='not_run' then 'not_run' when v.geometry_event_id is not null then 'stale_geometry' else 'complete' end,'split_from_instance_id',v.split_from_hd_animal_instance_id,
    'model_name',v.model_name,'model_version',v.model_version,'result',v.analysis,
    'created_at',v.result_created_at,'captured_at',v.captured_at,
    'camera_id',v.camera_id,'camera_name',v.camera_name,'workflow_state',coalesce(v.workflow_action,'active'),'workflow_reason',v.workflow_reason,'workflow_note',v.workflow_note,'topology_event_id',v.topology_event_id,
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
), latest_topology as (
 select distinct on (e.source_instance_id) e.source_instance_id,e.id topology_event_id,e.action topology_action
 from deerid.hd_instance_topology_events e order by e.source_instance_id,e.created_at desc,e.id desc
), base as (
 select i.id,i.created_at,m.camera_id,
   exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
   or exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null and not exists(select 1 from deerid.hd_instance_profile_assignment_events later where later.supersedes_event_id=e.id))
   or coalesce(topology.topology_action in ('split','remove','inseparable'),false) resolved,
   workflow.action workflow_action,proposal.proposal_state,topology.topology_action
 from latest_results r join deerid.hd_animal_instances i on i.hd_review_result_id=r.id join deerid.media m on m.id=i.media_id
 left join latest_topology topology on topology.source_instance_id=i.id
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
 'removed_detections',(select count(*) from base where topology_action='remove'),
 'inseparable',(select count(*) from base where topology_action='inseparable'),
 'by_camera',coalesce((select jsonb_object_agg(camera_id,cnt) from (select camera_id,count(*) cnt from unresolved where camera_id is not null group by camera_id) grouped),'{}'::jsonb)
);
$$;
revoke all on function public.deerid_hd_review_progress() from public,anon,authenticated;
grant execute on function public.deerid_hd_review_progress() to service_role;

create or replace function deerid.hd_instance_topology_terminal(p_instance_id uuid)
returns boolean language sql stable security definer set search_path=pg_catalog,deerid,pg_temp as $$
select coalesce((select e.action in ('split','remove','inseparable') from deerid.hd_instance_topology_events e where e.source_instance_id=p_instance_id order by e.created_at desc,e.id desc limit 1),false);
$$;
revoke all on function deerid.hd_instance_topology_terminal(uuid) from public,anon,authenticated,service_role;

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
    'pending_count',(select count(*) from deerid.hd_animal_instances i join deerid.hd_review_results r on r.id=i.hd_review_result_id where not exists(select 1 from deerid.hd_review_results newer where newer.media_asset_id=r.media_asset_id and (newer.created_at,newer.id)>(r.created_at,r.id)) and not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer') and not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null) and not deerid.hd_instance_topology_terminal(i.id)),
    'oldest_pending_at',(select min(i.created_at) from deerid.hd_animal_instances i join deerid.hd_review_results r on r.id=i.hd_review_result_id where not exists(select 1 from deerid.hd_review_results newer where newer.media_asset_id=r.media_asset_id and (newer.created_at,newer.id)>(r.created_at,r.id)) and not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer') and not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id and e.animal_profile_id is not null) and not deerid.hd_instance_topology_terminal(i.id)),
    'stale_claim_count',null,'failure_count_24h',null,'telemetry_complete',false)
  )
);
$$;
revoke all on function public.deerid_pipeline_health() from public,anon,authenticated;
grant execute on function public.deerid_pipeline_health() to service_role;
