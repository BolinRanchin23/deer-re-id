-- Fast returned-HD review, crop-first profile galleries, and append-only reassignment history.

create table deerid.hd_instance_profile_assignment_events (
  id bigint generated always as identity primary key,
  hd_animal_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  animal_profile_id uuid not null references deerid.animal_profiles(id) on delete restrict,
  action text not null check (action in ('assign','reassign')),
  supersedes_event_id bigint references deerid.hd_instance_profile_assignment_events(id) on delete restrict,
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  check ((action='assign' and supersedes_event_id is null) or (action='reassign' and supersedes_event_id is not null))
);
create unique index hd_instance_assignment_one_successor_idx
  on deerid.hd_instance_profile_assignment_events(supersedes_event_id) where supersedes_event_id is not null;
create index hd_instance_assignment_instance_idx
  on deerid.hd_instance_profile_assignment_events(hd_animal_instance_id,created_at desc,id desc);
alter table deerid.hd_instance_profile_assignment_events enable row level security;
revoke all on deerid.hd_instance_profile_assignment_events from public,anon,authenticated,service_role;
revoke all on sequence deerid.hd_instance_profile_assignment_events_id_seq from public,anon,authenticated,service_role;

create or replace function deerid.reject_hd_instance_assignment_event_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin raise exception 'HD instance assignment events are append-only'; end $$;
create trigger hd_instance_assignment_events_append_only before update or delete
on deerid.hd_instance_profile_assignment_events for each row execute function deerid.reject_hd_instance_assignment_event_mutation();
revoke all on function deerid.reject_hd_instance_assignment_event_mutation() from public,anon,authenticated;

insert into deerid.hd_instance_profile_assignment_events(hd_animal_instance_id,animal_profile_id,action,actor_id,created_at)
select a.hd_animal_instance_id,a.animal_profile_id,'assign',a.actor_id,a.created_at
from deerid.hd_instance_profile_assignments a
where not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=a.hd_animal_instance_id);

create or replace function public.deerid_claim_hd_review(
 p_model_name text,p_model_version text,p_media_asset_id uuid default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; token uuid:=gen_random_uuid();
begin
 delete from deerid.hd_review_claims where claimed_at<now()-interval '30 minutes';
 select * into chosen from deerid.media_assets a where a.variant='cloud_hd'
 and (p_media_asset_id is null or a.id=p_media_asset_id)
 and not exists(select 1 from deerid.hd_review_results r where r.media_asset_id=a.id and r.model_name=p_model_name and r.model_version=p_model_version)
 and not exists(select 1 from deerid.hd_review_failures f where f.media_asset_id=a.id)
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

create or replace function public.deerid_hd_review_queue(p_limit integer default 60)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with latest_results as (
 select distinct on (r.media_asset_id) r.* from deerid.hd_review_results r
 order by r.media_asset_id,r.created_at desc,r.id desc
), pending as (
 select i.*,r.model_name,r.model_version,r.created_at result_created_at,
   count(*) over(partition by i.hd_review_result_id)::integer instance_count
 from latest_results r join deerid.hd_animal_instances i on i.hd_review_result_id=r.id
 where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
 and not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=i.id)
 order by r.created_at,i.instance_index
 limit greatest(1,least(coalesce(p_limit,60),120))
)
select coalesce(jsonb_agg(jsonb_build_object(
 'hd_review_result_id',p.hd_review_result_id,'hd_animal_instance_id',p.id,'media_id',p.media_id,'media_asset_id',p.media_asset_id,
 'instance_index',p.instance_index,'instance_count',p.instance_count,
 'bbox',jsonb_build_object('x',p.bbox_x,'y',p.bbox_y,'width',p.bbox_width,'height',p.bbox_height),
 'crop_recipe',p.crop_recipe,'detection_complete',p.detection_complete,'detection_notes',p.detection_notes,
 'model_name',p.model_name,'model_version',p.model_version,'result',p.analysis,'created_at',p.result_created_at,
 'captured_at',m.captured_at,'camera_name',c.name
) order by p.result_created_at,p.instance_index),'[]'::jsonb)
from pending p join deerid.media m on m.id=p.media_id left join deerid.cameras c on c.id=m.camera_id;
$$;

create or replace function public.deerid_profile_gallery(p_limit integer default 200)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_events as (
 select distinct on (e.hd_animal_instance_id) e.* from deerid.hd_instance_profile_assignment_events e
 order by e.hd_animal_instance_id,e.created_at desc,e.id desc
), picked as (
 select am.animal_profile_id,am.media_id,m.captured_at,c.name camera_name,
   ce.id assignment_event_id,i.id hd_animal_instance_id,i.media_asset_id,
   case when i.id is null then null else jsonb_build_object('x',i.bbox_x,'y',i.bbox_y,'width',i.bbox_width,'height',i.bbox_height) end bbox,
   i.crop_recipe
 from deerid.animal_media am join deerid.media m on m.id=am.media_id left join deerid.cameras c on c.id=m.camera_id
 left join lateral (
   select e.* from current_events e join deerid.hd_animal_instances candidate_i on candidate_i.id=e.hd_animal_instance_id
   where e.animal_profile_id=am.animal_profile_id and candidate_i.media_id=am.media_id
   order by e.created_at desc,e.id desc limit 1
 ) ce on true
 left join deerid.hd_animal_instances i on i.id=ce.hd_animal_instance_id
 where am.confirmation_status='confirmed'
 order by m.captured_at desc,am.animal_profile_id,am.media_id
 limit greatest(1,least(coalesce(p_limit,200),500))
)
select coalesce(jsonb_agg(jsonb_build_object(
 'assignment_event_id',assignment_event_id,'hd_animal_instance_id',hd_animal_instance_id,
 'animal_profile_id',animal_profile_id,'media_id',media_id,'media_asset_id',media_asset_id,
 'captured_at',captured_at,'camera_name',camera_name,'bbox',bbox,'crop_recipe',crop_recipe
) order by captured_at desc,animal_profile_id,media_id),'[]'::jsonb) from picked;
$$;

create or replace function public.deerid_reassign_hd_instance(p_assignment_event_id bigint,p_profile_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare prior deerid.hd_instance_profile_assignment_events%rowtype; instance deerid.hd_animal_instances%rowtype;
 target_year integer; captured_year integer; new_event_id bigint;
begin
 select * into prior from deerid.hd_instance_profile_assignment_events where id=p_assignment_event_id for update;
 if prior.id is null or exists(select 1 from deerid.hd_instance_profile_assignment_events where supersedes_event_id=prior.id) then raise exception 'stale assignment'; end if;
 select * into instance from deerid.hd_animal_instances where id=prior.hd_animal_instance_id;
 select ap.season_year into target_year from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id where ap.id=p_profile_id and ap.active and a.status='active';
 select extract(year from captured_at)::integer into captured_year from deerid.media where id=instance.media_id;
 if target_year is null or target_year<>captured_year or p_profile_id=prior.animal_profile_id then raise exception 'invalid reassignment'; end if;
 insert into deerid.hd_instance_profile_assignment_events(hd_animal_instance_id,animal_profile_id,action,supersedes_event_id,actor_id)
 values(instance.id,p_profile_id,'reassign',prior.id,auth.uid()) returning id into new_event_id;
 insert into deerid.animal_media(animal_profile_id,media_id,match_source,match_confidence,confirmation_status,confirmed_by)
 values(p_profile_id,instance.media_id,'human',1,'confirmed',auth.uid())
 on conflict(animal_profile_id,media_id) do update set match_source='human',match_confidence=1,confirmation_status='confirmed',confirmed_by=auth.uid();
 if not exists(
   select 1 from deerid.hd_animal_instances other_i join lateral (
     select e.animal_profile_id from deerid.hd_instance_profile_assignment_events e where e.hd_animal_instance_id=other_i.id order by e.created_at desc,e.id desc limit 1
   ) current_event on current_event.animal_profile_id=prior.animal_profile_id
   where other_i.media_id=instance.media_id
 ) then delete from deerid.animal_media where animal_profile_id=prior.animal_profile_id and media_id=instance.media_id; end if;
 return jsonb_build_object('ok',true,'assignment_event_id',new_event_id,'profile_id',p_profile_id,'hd_animal_instance_id',instance.id);
end $$;

create or replace function public.deerid_record_hd_review_decision(
 p_hd_review_result_id bigint,p_action text,p_profile_id uuid default null,p_display_name text default null,
 p_species text default null,p_sex text default null,p_note text default null,p_hd_animal_instance_id uuid default null
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare instance deerid.hd_animal_instances%rowtype; selected_profile uuid; selected_animal uuid; captured_year integer; decision_id bigint;
begin
 if p_action not in ('create_profile','match_profile','not_identity_worthy','defer') or length(coalesce(p_note,''))>500 then raise exception 'invalid HD review decision'; end if;
 select * into instance from deerid.hd_animal_instances where id=p_hd_animal_instance_id and hd_review_result_id=p_hd_review_result_id for update;
 if instance.id is null then raise exception 'animal instance not found'; end if;
 if p_action<>'defer' and exists(select 1 from deerid.hd_review_decisions where hd_animal_instance_id=instance.id and action<>'defer') then
  select animal_profile_id into selected_profile from deerid.hd_review_decisions where hd_animal_instance_id=instance.id and action<>'defer' limit 1;
  return jsonb_build_object('ok',true,'replayed',true,'profile_id',selected_profile,'hd_animal_instance_id',instance.id);
 end if;
 select extract(year from captured_at)::integer into captured_year from deerid.media where id=instance.media_id;
 if p_action='create_profile' then
  if length(trim(coalesce(p_display_name,''))) not between 1 and 80 or p_species not in ('white-tailed deer','axis deer','other deer') or p_sex not in ('male','female','unknown') then raise exception 'invalid deer profile'; end if;
  insert into deerid.animals(species,display_name,sex,notes) values(p_species,trim(p_display_name),nullif(p_sex,'unknown'),p_note) returning id into selected_animal;
  insert into deerid.animal_profiles(animal_id,season_year) values(selected_animal,captured_year) returning id into selected_profile;
 elsif p_action='match_profile' then
  select ap.id into selected_profile from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id where ap.id=p_profile_id and ap.active and a.status='active' and ap.season_year=captured_year;
  if selected_profile is null then raise exception 'invalid profile assignment'; end if;
 end if;
 insert into deerid.hd_review_decisions(hd_review_result_id,hd_animal_instance_id,action,animal_profile_id,note)
 values(instance.hd_review_result_id,instance.id,p_action,selected_profile,nullif(trim(coalesce(p_note,'')),'')) returning id into decision_id;
 if selected_profile is not null then
  insert into deerid.animal_media(animal_profile_id,media_id,match_source,match_confidence,confirmation_status,confirmed_by)
  values(selected_profile,instance.media_id,'human',1,'confirmed',auth.uid()) on conflict(animal_profile_id,media_id) do update set match_source='human',match_confidence=1,confirmation_status='confirmed',confirmed_by=auth.uid();
  insert into deerid.hd_instance_profile_assignments(hd_animal_instance_id,animal_profile_id,hd_review_decision_id,actor_id) values(instance.id,selected_profile,decision_id,auth.uid());
  insert into deerid.hd_instance_profile_assignment_events(hd_animal_instance_id,animal_profile_id,action,actor_id) values(instance.id,selected_profile,'assign',auth.uid());
 end if;
 return jsonb_build_object('ok',true,'action',p_action,'profile_id',selected_profile,'hd_animal_instance_id',instance.id);
end $$;

revoke all on function public.deerid_claim_hd_review(text,text,uuid) from public,anon,authenticated;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
revoke all on function public.deerid_profile_gallery(integer) from public,anon,authenticated;
revoke all on function public.deerid_reassign_hd_instance(bigint,uuid) from public,anon,authenticated;
revoke all on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text,uuid) from public,anon,authenticated;
grant execute on function public.deerid_claim_hd_review(text,text,uuid) to service_role;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;
grant execute on function public.deerid_profile_gallery(integer) to service_role;
grant execute on function public.deerid_reassign_hd_instance(bigint,uuid) to service_role;
grant execute on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text,uuid) to service_role;
