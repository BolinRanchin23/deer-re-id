-- Pin process/photo evidence and restore representative profile imagery with current-assignment validity.
create or replace function public.deerid_process_overview() returns jsonb
language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with bounds as (select now() as as_of), windows as (
 select 'last_24_hours' key,as_of-interval '24 hours' start_at,as_of end_at from bounds union all
 select 'last_7_days',as_of-interval '7 days',as_of from bounds
), authoritative_gate1b as (
 select distinct on (p.media_id) p.* from deerid.gate1b_predictions p
 where p.model_name='OpenAI-GPT-4o-mini-Vision'
 and p.model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'
 order by p.media_id,p.created_at desc,p.id desc
), current_assignments as (
 select e.* from deerid.hd_instance_profile_assignment_events e
 where not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)
), values_by_window as (
 select w.*,
  (select count(*) from deerid.media m where m.ingested_at>=w.start_at and m.ingested_at<w.end_at)::int photos_received,
  (select count(*) from authoritative_gate1b p where p.created_at>=w.start_at and p.created_at<w.end_at and (p.visible_antler='yes' or p.probable_male='yes'))::int male_or_antler,
  (select count(*) from deerid.hd_animal_instances i where i.created_at>=w.start_at and i.created_at<w.end_at)::int animal_crops,
  (select count(*) from deerid.hd_requests r where r.created_at>=w.start_at and r.created_at<w.end_at)::int hd_requests,
  (select count(distinct e.animal_profile_id) from current_assignments e where e.created_at>=w.start_at and e.created_at<w.end_at)::int profiles
 from windows w
)
select jsonb_build_object('as_of',(select as_of from bounds),
 'last_24_hours',(select jsonb_build_object('from',start_at,'to',end_at,'photos_received',photos_received,'male_or_antler',male_or_antler,'animal_crops',animal_crops,'hd_requests',hd_requests,'profiles',profiles) from values_by_window where key='last_24_hours'),
 'last_7_days',(select jsonb_build_object('from',start_at,'to',end_at,'photos_received',photos_received,'male_or_antler',male_or_antler,'animal_crops',animal_crops,'hd_requests',hd_requests,'profiles',profiles) from values_by_window where key='last_7_days'));
$$;
revoke all on function public.deerid_process_overview() from public,anon,authenticated;
grant execute on function public.deerid_process_overview() to service_role;

create or replace function public.deerid_profiles() returns jsonb
language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
with current_assignments as (
 select e.* from deerid.hd_instance_profile_assignment_events e
 where not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)
), current_representatives as (
 select distinct on (r.animal_profile_id) r.* from deerid.profile_representative_events r
 where not exists(select 1 from deerid.profile_representative_events n where n.supersedes_event_id=r.id)
 order by r.animal_profile_id,r.created_at desc,r.id desc
), profile_rows as (
 select ap.id,a.id animal_id,a.display_name,a.species,coalesce(a.sex,'unknown') sex,ap.season_year,
  counts.photo_count,counts.first_seen,counts.last_seen,counts.camera_ids,counts.camera_names,
  previews.items profile_previews,previews.representative_assignment_event_id
 from deerid.animal_profiles ap join deerid.animals a on a.id=ap.animal_id
 left join lateral (
  select count(am.media_id)::int photo_count,min(m.captured_at) first_seen,max(m.captured_at) last_seen,
   coalesce(jsonb_agg(distinct m.camera_id) filter(where m.camera_id is not null),'[]'::jsonb) camera_ids,
   coalesce(jsonb_agg(distinct c.name) filter(where c.name is not null),'[]'::jsonb) camera_names
  from deerid.animal_media am join deerid.media m on m.id=am.media_id left join deerid.cameras c on c.id=m.camera_id
  where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
 ) counts on true
 left join lateral (
  select coalesce(jsonb_agg(jsonb_build_object('media_id',picked.media_id,'captured_at',picked.captured_at) order by picked.is_representative desc,picked.captured_at desc),'[]'::jsonb) items,
   max(picked.assignment_event_id) filter(where picked.is_representative) representative_assignment_event_id
  from (
   select am.media_id,m.captured_at,ce.id assignment_event_id,
    (cr.assignment_event_id=ce.id and cr.animal_profile_id=ap.id)::boolean is_representative
   from deerid.animal_media am join deerid.media m on m.id=am.media_id
   left join deerid.hd_animal_instances i on i.media_id=am.media_id
   left join current_assignments ce on ce.hd_animal_instance_id=i.id and ce.animal_profile_id=ap.id
   left join current_representatives cr on cr.animal_profile_id=ap.id and cr.assignment_event_id=ce.id
   where am.animal_profile_id=ap.id and am.confirmation_status='confirmed'
   order by is_representative desc,m.captured_at desc limit 5
  ) picked
 ) previews on true
 where ap.active and a.status='active'
)
select coalesce(jsonb_agg(to_jsonb(profile_rows) order by display_name),'[]'::jsonb) from profile_rows;
$$;
revoke all on function public.deerid_profiles() from public,anon,authenticated;
grant execute on function public.deerid_profiles() to service_role;

create or replace function public.deerid_set_profile_representative(p_assignment_event_id bigint,p_profile_id uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
declare prior bigint; new_id bigint;
begin
 if not exists(
  select 1 from deerid.hd_instance_profile_assignment_events e
  join deerid.hd_animal_instances i on i.id=e.hd_animal_instance_id
  join deerid.animal_media am on am.media_id=i.media_id and am.animal_profile_id=e.animal_profile_id and am.confirmation_status='confirmed'
  where e.id=p_assignment_event_id and e.animal_profile_id=p_profile_id
  and not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)
 ) then raise exception 'ineligible representative'; end if;
 select id into prior from deerid.profile_representative_events r where r.animal_profile_id=p_profile_id
 and not exists(select 1 from deerid.profile_representative_events n where n.supersedes_event_id=r.id)
 order by r.created_at desc,r.id desc limit 1;
 insert into deerid.profile_representative_events(animal_profile_id,assignment_event_id,supersedes_event_id,actor_id)
 values(p_profile_id,p_assignment_event_id,prior,auth.uid()) returning id into new_id;
 return jsonb_build_object('ok',true,'profile_id',p_profile_id,'assignment_event_id',p_assignment_event_id,'event_id',new_id);
end $$;
revoke all on function public.deerid_set_profile_representative(bigint,uuid) from public,anon,authenticated;
grant execute on function public.deerid_set_profile_representative(bigint,uuid) to service_role;


-- Correct All Photos filters, sort modes, total semantics, and keyset pagination.
create or replace function public.deerid_all_photos(
  p_limit integer default 30,
  p_cursor text default null,
  p_date_from date default null,
  p_date_to date default null,
  p_hour_from integer default null,
  p_hour_to integer default null,
  p_time_of_day text default 'all',
  p_camera_id uuid default null,
  p_species text default null,
  p_male_antler text default null,
  p_profile_status text default null,
  p_variant text default null,
  p_identity_status text default null,
  p_sort text default 'newest'
) returns jsonb
language sql stable security definer
set search_path=pg_catalog,public,deerid,pg_temp
as $$
with latest_prediction as (
  select distinct on (p.media_id)
    p.media_id,p.species_label,p.visible_antler,p.probable_male,p.triage_class
  from deerid.gate1b_predictions p
  where p.model_name='OpenAI-GPT-4o-mini-Vision'
    and p.model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'
  order by p.media_id,p.created_at desc,p.id desc
), base as (
  select m.id,m.captured_at,m.camera_id,c.name camera_name,m.variant,
    p.species_label,p.visible_antler,p.probable_male,p.triage_class,
    exists(
      select 1 from deerid.animal_media am
      where am.media_id=m.id and am.confirmation_status='confirmed'
    ) has_confirmed_profile,
    to_char(m.captured_at at time zone 'America/Chicago','HH24MISS.US') time_key,
    to_char(m.captured_at,'YYYY-MM-DD"T"HH24:MI:SS.USOF') captured_key
  from deerid.media m
  left join deerid.cameras c on c.id=m.camera_id
  left join latest_prediction p on p.media_id=m.id
  where (p_camera_id is null or m.camera_id=p_camera_id)
    and (p_date_from is null or (m.captured_at at time zone 'America/Chicago')::date>=p_date_from)
    and (p_date_to is null or (m.captured_at at time zone 'America/Chicago')::date<=p_date_to)
    and (p_hour_from is null or extract(hour from m.captured_at at time zone 'America/Chicago')>=p_hour_from)
    and (p_hour_to is null or extract(hour from m.captured_at at time zone 'America/Chicago')<=p_hour_to)
    and (p_time_of_day='all' or (p_time_of_day='day' and extract(hour from m.captured_at at time zone 'America/Chicago') between 6 and 19) or (p_time_of_day='night' and not (extract(hour from m.captured_at at time zone 'America/Chicago') between 6 and 19)))
    and (p_variant is null or m.variant=p_variant)
    and (p_species is null or (p_species='deer' and p.species_label in ('whitetail','axis','other_deer')) or p.species_label=p_species)
    and (p_male_antler is null or (p_male_antler='yes' and (p.visible_antler='yes' or p.probable_male='yes')) or (p_male_antler='no' and coalesce(p.visible_antler,'unknown')<>'yes' and coalesce(p.probable_male,'unknown')<>'yes'))
    and (p_profile_status is null or (p_profile_status in ('assigned','profiled') and exists(select 1 from deerid.animal_media am where am.media_id=m.id and am.confirmation_status='confirmed')) or (p_profile_status in ('unassigned','unprofiled') and not exists(select 1 from deerid.animal_media am where am.media_id=m.id and am.confirmation_status='confirmed')))
    and (p_identity_status is null or (p_identity_status='viable' and p.triage_class in ('likely_male','uncertain')) or (p_identity_status='not_viable' and p.triage_class='female_candidate'))
), keyed as (
  select *,case when p_sort in ('time_asc','time_desc') then time_key||'|'||captured_key||'|'||id::text else captured_key||'|'||id::text end page_key
  from base
), after_cursor as (
  select * from keyed
  where p_cursor is null
    or (p_sort in ('oldest','time_asc') and page_key>p_cursor)
    or (p_sort in ('newest','time_desc') and page_key<p_cursor)
), ordered_page as (
  select * from after_cursor
  order by
    case when p_sort in ('oldest','time_asc') then page_key end asc,
    case when p_sort in ('newest','time_desc') then page_key end desc
  limit greatest(1,least(p_limit,60))
)
select jsonb_build_object(
  'items',coalesce((select jsonb_agg(jsonb_build_object(
    'id',id,'captured_at',captured_at,'camera_id',camera_id,'camera_name',camera_name,
    'variant',variant,'species_label',species_label,'visible_antler',visible_antler,
    'probable_male',probable_male,'profile_status',case when has_confirmed_profile then 'assigned' else 'unassigned' end
  ) order by case when p_sort in ('oldest','time_asc') then page_key end asc,case when p_sort in ('newest','time_desc') then page_key end desc) from ordered_page),'[]'::jsonb),
  'next_cursor',case when (select count(*) from ordered_page)=greatest(1,least(p_limit,60)) and exists(
    select 1 from after_cursor a where
      (p_sort in ('oldest','time_asc') and a.page_key>(select max(page_key) from ordered_page))
      or (p_sort in ('newest','time_desc') and a.page_key<(select min(page_key) from ordered_page))
  ) then (select page_key from ordered_page order by case when p_sort in ('oldest','time_asc') then page_key end desc,case when p_sort in ('newest','time_desc') then page_key end asc limit 1) end,
  'total',(select count(*) from base),
  'facets',jsonb_build_object()
);
$$;
revoke all on function public.deerid_all_photos(integer,text,date,date,integer,integer,text,uuid,text,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.deerid_all_photos(integer,text,date,date,integer,integer,text,uuid,text,text,text,text,text,text) to service_role;
