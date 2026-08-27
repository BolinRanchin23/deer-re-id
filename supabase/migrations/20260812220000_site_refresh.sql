-- DeerID site organization refresh: bounded metrics, paged archive, locations, profile summaries and representatives.
create table deerid.profile_representative_events (
 id bigint generated always as identity primary key,
 animal_profile_id uuid not null references deerid.animal_profiles(id) on delete restrict,
 assignment_event_id bigint not null references deerid.hd_instance_profile_assignment_events(id) on delete restrict,
 supersedes_event_id bigint references deerid.profile_representative_events(id) on delete restrict,
 actor_id uuid references auth.users(id) on delete set null,
 created_at timestamptz not null default now()
);
create unique index profile_representative_successor_idx on deerid.profile_representative_events(supersedes_event_id) where supersedes_event_id is not null;
alter table deerid.profile_representative_events enable row level security;
revoke all on deerid.profile_representative_events from public,anon,authenticated,service_role;

create or replace function public.deerid_process_overview() returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with bounds as (select now() as as_of), windows as (
 select 'last_24_hours' key,as_of-interval '24 hours' start_at,as_of end_at from bounds union all
 select 'last_7_days',as_of-interval '7 days',as_of from bounds
), values_by_window as (
 select w.*,
  (select count(*) from deerid.media m where m.ingested_at>=w.start_at and m.ingested_at<w.end_at)::int photos_received,
  (select count(distinct p.media_id) from deerid.gate1b_predictions p where p.created_at>=w.start_at and p.created_at<w.end_at and (p.visible_antler='yes' or p.probable_male='yes'))::int male_or_antler,
  (select count(*) from deerid.hd_animal_instances i where i.created_at>=w.start_at and i.created_at<w.end_at)::int animal_crops,
  (select count(*) from deerid.hd_requests r where r.created_at>=w.start_at and r.created_at<w.end_at)::int hd_requests,
  (select count(distinct e.animal_profile_id) from deerid.hd_instance_profile_assignment_events e join deerid.hd_animal_instances i on i.id=e.hd_animal_instance_id where e.created_at>=w.start_at and e.created_at<w.end_at)::int profiles
 from windows w
)
select jsonb_build_object('as_of',(select as_of from bounds),'last_24_hours',(select jsonb_build_object('from',start_at,'to',end_at,'photos_received',photos_received,'male_or_antler',male_or_antler,'animal_crops',animal_crops,'hd_requests',hd_requests,'profiles',profiles) from values_by_window where key='last_24_hours'),'last_7_days',(select jsonb_build_object('from',start_at,'to',end_at,'photos_received',photos_received,'male_or_antler',male_or_antler,'animal_crops',animal_crops,'hd_requests',hd_requests,'profiles',profiles) from values_by_window where key='last_7_days'));
$$;

create or replace function public.deerid_all_photos(p_limit integer default 30,p_cursor text default null,p_date_from date default null,p_date_to date default null,p_hour_from integer default null,p_hour_to integer default null,p_time_of_day text default 'all',p_camera_id uuid default null,p_species text default null,p_male_antler text default null,p_profile_status text default null,p_variant text default null,p_identity_status text default null,p_sort text default 'newest') returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with filtered as (
 select m.*,c.name camera_name from deerid.media m left join deerid.cameras c on c.id=m.camera_id
 where (p_camera_id is null or m.camera_id=p_camera_id)
 and (p_date_from is null or (m.captured_at at time zone 'America/Chicago')::date>=p_date_from)
 and (p_date_to is null or (m.captured_at at time zone 'America/Chicago')::date<=p_date_to)
 and (p_hour_from is null or extract(hour from m.captured_at at time zone 'America/Chicago')>=p_hour_from)
 and (p_hour_to is null or extract(hour from m.captured_at at time zone 'America/Chicago')<=p_hour_to)
 and (p_variant is null or m.variant=p_variant)
 and (p_cursor is null or (p_sort='oldest' and (m.captured_at::text||'|'||m.id::text)>p_cursor) or (p_sort<>'oldest' and (m.captured_at::text||'|'||m.id::text)<p_cursor))
), page as (select * from filtered order by case when p_sort='oldest' then captured_at end asc,case when p_sort<>'oldest' then captured_at end desc,id desc limit greatest(1,least(p_limit,60))+1)
select jsonb_build_object('items',coalesce((select jsonb_agg(jsonb_build_object('id',id,'captured_at',captured_at,'camera_id',camera_id,'camera_name',camera_name,'variant',variant) order by case when p_sort='oldest' then captured_at end asc,case when p_sort<>'oldest' then captured_at end desc) from (select * from page limit greatest(1,least(p_limit,60))) q),'[]'::jsonb),'next_cursor',(select captured_at::text||'|'||id::text from page offset greatest(1,least(p_limit,60)) limit 1),'total',(select count(*) from filtered),'facets',jsonb_build_object());
$$;

create or replace function public.deerid_set_profile_representative(p_assignment_event_id bigint,p_profile_id uuid) returns jsonb language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare prior bigint; new_id bigint;
begin
 if not exists(select 1 from deerid.hd_instance_profile_assignment_events e where e.id=p_assignment_event_id and e.animal_profile_id=p_profile_id) then raise exception 'ineligible representative'; end if;
 select id into prior from deerid.profile_representative_events where animal_profile_id=p_profile_id and not exists(select 1 from deerid.profile_representative_events n where n.supersedes_event_id=profile_representative_events.id) order by created_at desc,id desc limit 1;
 insert into deerid.profile_representative_events(animal_profile_id,assignment_event_id,supersedes_event_id,actor_id) values(p_profile_id,p_assignment_event_id,prior,auth.uid()) returning id into new_id;
 return jsonb_build_object('ok',true,'profile_id',p_profile_id,'assignment_event_id',p_assignment_event_id,'event_id',new_id);
end $$;

-- Replace profiles contract with factual summaries and any-confirmed-photo camera membership.
create or replace function public.deerid_profiles() returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with profile_rows as (
 select ap.id, a.id animal_id, a.display_name, a.species, coalesce(a.sex,'unknown') sex,
  ap.season_year, count(am.media_id)::int photo_count, min(m.captured_at) first_seen,
  max(m.captured_at) last_seen,
  coalesce(jsonb_agg(distinct m.camera_id) filter (where m.camera_id is not null),'[]'::jsonb) camera_ids,
  coalesce(jsonb_agg(distinct c.name) filter (where c.name is not null),'[]'::jsonb) camera_names,
  coalesce(
   (select r.assignment_event_id from deerid.profile_representative_events r where r.animal_profile_id=ap.id order by r.created_at desc,r.id desc limit 1),
   (select e.id from deerid.hd_instance_profile_assignment_events e join deerid.hd_animal_instances i on i.id=e.hd_animal_instance_id where e.animal_profile_id=ap.id order by i.created_at desc,e.id desc limit 1)
  ) representative_assignment_event_id
 from deerid.animal_profiles ap
 join deerid.animals a on a.id=ap.animal_id
 left join deerid.animal_media am on am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
 left join deerid.media m on m.id=am.media_id
 left join deerid.cameras c on c.id=m.camera_id
 where ap.active
 group by ap.id,a.id
)
select coalesce(jsonb_agg(to_jsonb(profile_rows) order by display_name),'[]'::jsonb) from profile_rows;
$$;
revoke all on function public.deerid_process_overview() from public,anon,authenticated;
revoke all on function public.deerid_all_photos(integer,text,date,date,integer,integer,text,uuid,text,text,text,text,text,text) from public,anon,authenticated;
revoke all on function public.deerid_set_profile_representative(bigint,uuid) from public,anon,authenticated;
grant execute on function public.deerid_process_overview() to service_role;
grant execute on function public.deerid_all_photos(integer,text,date,date,integer,integer,text,uuid,text,text,text,text,text,text) to service_role;
grant execute on function public.deerid_set_profile_representative(bigint,uuid) to service_role;
