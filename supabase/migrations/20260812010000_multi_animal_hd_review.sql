-- Multi-animal returned-HD support: one immutable detector instance and one human decision per deer.

create table deerid.hd_animal_instances (
  id uuid primary key default gen_random_uuid(),
  hd_review_result_id bigint not null references deerid.hd_review_results(id) on delete restrict,
  media_id uuid not null references deerid.media(id) on delete restrict,
  media_asset_id uuid not null references deerid.media_assets(id) on delete restrict,
  instance_index integer not null check (instance_index between 1 and 20),
  bbox_x double precision not null check (bbox_x between 0 and 1),
  bbox_y double precision not null check (bbox_y between 0 and 1),
  bbox_width double precision not null check (bbox_width > 0 and bbox_width <= 1),
  bbox_height double precision not null check (bbox_height > 0 and bbox_height <= 1),
  detection_complete boolean not null,
  detection_notes text not null check (length(detection_notes) between 1 and 500),
  analysis jsonb not null,
  crop_recipe jsonb not null,
  created_at timestamptz not null default now(),
  unique (hd_review_result_id, instance_index),
  check (bbox_x + bbox_width <= 1),
  check (bbox_y + bbox_height <= 1)
);
create index hd_animal_instances_asset_idx on deerid.hd_animal_instances(media_asset_id, instance_index);
alter table deerid.hd_animal_instances enable row level security;
grant select, insert on deerid.hd_animal_instances to service_role;
revoke update, delete, truncate on deerid.hd_animal_instances from service_role;

create or replace function deerid.reject_hd_animal_instance_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin raise exception 'HD animal instance evidence is append-only'; end $$;
create trigger hd_animal_instances_append_only before update or delete on deerid.hd_animal_instances
for each row execute function deerid.reject_hd_animal_instance_mutation();
revoke all on function deerid.reject_hd_animal_instance_mutation() from public,anon,authenticated;

-- Preserve old single-animal results as explicit whole-frame instances.
insert into deerid.hd_animal_instances(
  hd_review_result_id,media_id,media_asset_id,instance_index,bbox_x,bbox_y,bbox_width,bbox_height,
  detection_complete,detection_notes,analysis,crop_recipe
)
select r.id,r.media_id,r.media_asset_id,1,0,0,1,1,
  coalesce((r.result->>'animal_count')::integer,1)<=1,
  case when coalesce((r.result->>'animal_count')::integer,1)<=1 then 'legacy single-animal whole-frame result' else 'legacy multi-animal result requires manual separation' end,
  r.result,
  jsonb_build_object('kind','normalized_bbox','padding',0,'source','legacy_whole_frame')
from deerid.hd_review_results r
where not exists(select 1 from deerid.hd_animal_instances i where i.hd_review_result_id=r.id);

alter table deerid.hd_review_decisions add column hd_animal_instance_id uuid references deerid.hd_animal_instances(id) on delete restrict;
drop trigger hd_review_decisions_append_only on deerid.hd_review_decisions;
update deerid.hd_review_decisions d set hd_animal_instance_id=i.id
from deerid.hd_animal_instances i where i.hd_review_result_id=d.hd_review_result_id and i.instance_index=1 and d.hd_animal_instance_id is null;
create trigger hd_review_decisions_append_only before update or delete on deerid.hd_review_decisions for each row execute function deerid.reject_hd_review_mutation();
alter table deerid.hd_review_decisions alter column hd_animal_instance_id set not null;
drop index if exists deerid.hd_review_decisions_final_once_idx;
create unique index hd_review_decisions_final_once_idx on deerid.hd_review_decisions(hd_animal_instance_id) where action <> 'defer';

create table deerid.hd_instance_profile_assignments (
  id bigint generated always as identity primary key,
  hd_animal_instance_id uuid not null references deerid.hd_animal_instances(id) on delete restrict,
  animal_profile_id uuid not null references deerid.animal_profiles(id) on delete restrict,
  hd_review_decision_id bigint not null references deerid.hd_review_decisions(id) on delete restrict,
  actor_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (hd_animal_instance_id)
);
alter table deerid.hd_instance_profile_assignments enable row level security;
revoke all on deerid.hd_instance_profile_assignments from public,anon,authenticated,service_role;
revoke all on sequence deerid.hd_instance_profile_assignments_id_seq from public,anon,authenticated,service_role;

create or replace function deerid.reject_hd_instance_assignment_mutation()
returns trigger language plpgsql set search_path=pg_catalog,deerid,pg_temp as $$
begin raise exception 'HD animal instance assignments are append-only'; end $$;
create trigger hd_instance_profile_assignments_append_only before update or delete on deerid.hd_instance_profile_assignments
for each row execute function deerid.reject_hd_instance_assignment_mutation();
revoke all on function deerid.reject_hd_instance_assignment_mutation() from public,anon,authenticated;

create or replace function public.deerid_complete_hd_review(p_claim_token uuid,p_model_name text,p_model_version text,p_result jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare chosen deerid.media_assets%rowtype; inserted_id bigint; animal jsonb; expected_index integer:=1; animal_total integer; detection_ok boolean; notes text;
begin
 select a.* into chosen from deerid.media_assets a join deerid.hd_review_claims q on q.media_asset_id=a.id where q.claim_token=p_claim_token for update of q;
 if chosen.id is null then raise exception 'stale HD review claim'; end if;
 if jsonb_typeof(p_result)<>'object' or jsonb_typeof(p_result->'animals')<>'array' then raise exception 'invalid animal instance result'; end if;
 animal_total:=jsonb_array_length(p_result->'animals');
 if animal_total<>coalesce((p_result->>'animal_count')::integer,-1) or animal_total>20 then raise exception 'animal count mismatch'; end if;
 detection_ok:=coalesce((p_result->>'detection_complete')::boolean,false);
 notes:=trim(coalesce(p_result->>'detection_notes',''));
 if length(notes) not between 1 and 500 then raise exception 'invalid detection notes'; end if;
 insert into deerid.hd_review_results(media_id,media_asset_id,model_name,model_version,result)
 values(chosen.media_id,chosen.id,p_model_name,p_model_version,p_result)
 on conflict(media_asset_id,model_name,model_version) do nothing returning id into inserted_id;
 if inserted_id is not null then
  for animal in select value from jsonb_array_elements(p_result->'animals') loop
   if coalesce((animal->>'instance_index')::integer,0)<>expected_index then raise exception 'invalid animal instance order'; end if;
   insert into deerid.hd_animal_instances(
    hd_review_result_id,media_id,media_asset_id,instance_index,bbox_x,bbox_y,bbox_width,bbox_height,
    detection_complete,detection_notes,analysis,crop_recipe
   ) values(
    inserted_id,chosen.media_id,chosen.id,expected_index,
    (animal#>>'{bbox,x}')::double precision,(animal#>>'{bbox,y}')::double precision,
    (animal#>>'{bbox,width}')::double precision,(animal#>>'{bbox,height}')::double precision,
    detection_ok,notes,animal-'instance_index'-'bbox',
    jsonb_build_object('kind','normalized_bbox','bbox',animal->'bbox','padding',0.08,'source_asset_id',chosen.id)
   );
   expected_index:=expected_index+1;
  end loop;
 end if;
 delete from deerid.hd_review_claims where claim_token=p_claim_token;
 return jsonb_build_object('ok',true,'inserted',inserted_id is not null,'animal_instances',animal_total);
end $$;

create or replace function public.deerid_hd_review_queue(p_limit integer default 60)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with pending as (
 select i.*,r.model_name,r.model_version,r.created_at as result_created_at,
   count(*) over(partition by i.hd_review_result_id)::integer as instance_count
 from deerid.hd_animal_instances i join deerid.hd_review_results r on r.id=i.hd_review_result_id
 where not exists(select 1 from deerid.hd_review_decisions d where d.hd_animal_instance_id=i.id and d.action<>'defer')
 order by r.created_at,i.instance_index
 limit greatest(1,least(coalesce(p_limit,60),120))
)
select coalesce(jsonb_agg(jsonb_build_object(
 'hd_review_result_id',p.hd_review_result_id,'hd_animal_instance_id',p.id,'media_id',p.media_id,'media_asset_id',p.media_asset_id,
 'instance_index',p.instance_index,'instance_count',p.instance_count,
 'bbox',jsonb_build_object('x',p.bbox_x,'y',p.bbox_y,'width',p.bbox_width,'height',p.bbox_height),
 'detection_complete',p.detection_complete,'detection_notes',p.detection_notes,
 'model_name',p.model_name,'model_version',p.model_version,'result',p.analysis,'created_at',p.result_created_at,
 'captured_at',m.captured_at,'camera_name',c.name
) order by p.result_created_at,p.instance_index),'[]'::jsonb)
from pending p join deerid.media m on m.id=p.media_id left join deerid.cameras c on c.id=m.camera_id;
$$;

create or replace function public.deerid_record_hd_review_decision(
 p_hd_review_result_id bigint, p_action text, p_profile_id uuid default null, p_display_name text default null,
 p_species text default null, p_sex text default null, p_note text default null,
 p_hd_animal_instance_id uuid default null
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
  select ap.id into selected_profile from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
  where ap.id=p_profile_id and ap.active and a.status='active' and ap.season_year=captured_year;
  if selected_profile is null then raise exception 'invalid profile assignment'; end if;
 end if;
 insert into deerid.hd_review_decisions(hd_review_result_id,hd_animal_instance_id,action,animal_profile_id,note)
 values(instance.hd_review_result_id,instance.id,p_action,selected_profile,nullif(trim(coalesce(p_note,'')),'')) returning id into decision_id;
 if selected_profile is not null then
  insert into deerid.animal_media(animal_profile_id,media_id,match_source,match_confidence,confirmation_status,confirmed_by)
  values(selected_profile,instance.media_id,'human',1,'confirmed',auth.uid())
  on conflict(animal_profile_id,media_id) do update set match_source='human',match_confidence=1,confirmation_status='confirmed',confirmed_by=auth.uid();
  insert into deerid.hd_instance_profile_assignments(hd_animal_instance_id,animal_profile_id,hd_review_decision_id,actor_id)
  values(instance.id,selected_profile,decision_id,auth.uid());
 end if;
 return jsonb_build_object('ok',true,'action',p_action,'profile_id',selected_profile,'hd_animal_instance_id',instance.id);
end $$;

revoke all on function public.deerid_complete_hd_review(uuid,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.deerid_hd_review_queue(integer) from public,anon,authenticated;
revoke all on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text,uuid) from public,anon,authenticated;
grant execute on function public.deerid_complete_hd_review(uuid,text,text,jsonb) to service_role;
grant execute on function public.deerid_hd_review_queue(integer) to service_role;
grant execute on function public.deerid_record_hd_review_decision(bigint,text,uuid,text,text,text,text,uuid) to service_role;
