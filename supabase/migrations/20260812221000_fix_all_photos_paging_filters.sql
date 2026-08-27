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
