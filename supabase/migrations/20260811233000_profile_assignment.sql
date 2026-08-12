-- Human-confirmed deer profile creation and season-scoped media assignment.

create or replace function public.deerid_profiles()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'id', ap.id,
        'animal_id', a.id,
        'display_name', a.display_name,
        'species', a.species,
        'sex', coalesce(a.sex, 'unknown'),
        'season_year', ap.season_year,
        'photo_count', coalesce(counts.photo_count, 0)
      )
      order by a.display_name, ap.season_year desc, ap.id
    ),
    '[]'::jsonb
  )
  from deerid.animal_profiles ap
  join deerid.animals a on a.id = ap.animal_id
  left join lateral (
    select count(*)::integer as photo_count
    from deerid.animal_media am
    where am.animal_profile_id = ap.id
      and am.confirmation_status = 'confirmed'
  ) counts on true
  where a.status = 'active' and ap.active;
$$;

create or replace function public.deerid_create_profile_from_review(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_display_name text,
  p_species text,
  p_sex text,
  p_notes text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment_id bigint;
  media_captured_at timestamptz;
  review_state deerid.gate1_review_state%rowtype;
  animal_id uuid;
  profile_id uuid;
begin
  if p_media_id is null or p_assessment_id is null or p_review_version < 0
     or length(trim(coalesce(p_display_name, ''))) not between 1 and 80
     or p_species not in ('white-tailed deer', 'axis deer', 'other deer')
     or p_sex not in ('male', 'female', 'unknown')
     or length(coalesce(p_notes, '')) > 500 then
    raise exception 'invalid profile creation';
  end if;

  select a.id, m.captured_at into latest_assessment_id, media_captured_at
  from deerid.gate1_assessments a
  join deerid.media m on m.id = a.media_id
  where a.media_id = p_media_id and a.route = 'review' and a.is_representative
  order by a.created_at desc, a.id desc limit 1;
  select * into review_state from deerid.gate1_review_state
  where gate1_assessment_id = p_assessment_id;
  if latest_assessment_id is distinct from p_assessment_id
     or review_state.gate1_assessment_id is null
     or review_state.version <> p_review_version
     or review_state.pending_hd
     or review_state.resolved then
    raise exception 'stale profile capability';
  end if;

  insert into deerid.animals (species, display_name, sex, notes)
  values (p_species, trim(p_display_name), nullif(p_sex, 'unknown'), nullif(trim(coalesce(p_notes, '')), ''))
  returning id into animal_id;
  insert into deerid.animal_profiles (animal_id, season_year)
  values (animal_id, extract(year from media_captured_at)::integer)
  returning id into profile_id;
  insert into deerid.animal_media (
    animal_profile_id, media_id, match_source, match_confidence, confirmation_status
  ) values (profile_id, p_media_id, 'human', 1, 'confirmed');

  return jsonb_build_object(
    'ok', true, 'animal_id', animal_id, 'profile_id', profile_id,
    'season_year', extract(year from media_captured_at)::integer
  );
end;
$$;

create or replace function public.deerid_attach_media_to_profile_from_review(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_profile_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment_id bigint;
  media_captured_at timestamptz;
  review_state deerid.gate1_review_state%rowtype;
  profile_year integer;
  profile_active boolean;
begin
  if p_media_id is null or p_assessment_id is null or p_review_version < 0
     or p_profile_id is null then
    raise exception 'invalid profile assignment';
  end if;

  select a.id, m.captured_at into latest_assessment_id, media_captured_at
  from deerid.gate1_assessments a
  join deerid.media m on m.id = a.media_id
  where a.media_id = p_media_id and a.route = 'review' and a.is_representative
  order by a.created_at desc, a.id desc limit 1;
  select * into review_state from deerid.gate1_review_state
  where gate1_assessment_id = p_assessment_id;
  select ap.season_year, (ap.active and a.status = 'active')
    into profile_year, profile_active
  from deerid.animal_profiles ap
  join deerid.animals a on a.id = ap.animal_id
  where ap.id = p_profile_id;

  if latest_assessment_id is distinct from p_assessment_id
     or review_state.gate1_assessment_id is null
     or review_state.version <> p_review_version
     or review_state.pending_hd
     or review_state.resolved
     or not coalesce(profile_active, false)
     or profile_year is distinct from extract(year from media_captured_at)::integer then
    raise exception 'stale or incompatible profile capability';
  end if;

  insert into deerid.animal_media (
    animal_profile_id, media_id, match_source, match_confidence, confirmation_status
  ) values (p_profile_id, p_media_id, 'human', 1, 'confirmed')
  on conflict (animal_profile_id, media_id) do update set
    match_source = 'human', match_confidence = 1,
    confirmation_status = 'confirmed', confirmed_by = null;

  return jsonb_build_object(
    'ok', true, 'profile_id', p_profile_id,
    'season_year', profile_year
  );
end;
$$;

revoke all on function public.deerid_profiles() from public, anon, authenticated;
grant execute on function public.deerid_profiles() to service_role;
revoke all on function public.deerid_create_profile_from_review(uuid, bigint, integer, text, text, text, text) from public, anon, authenticated;
grant execute on function public.deerid_create_profile_from_review(uuid, bigint, integer, text, text, text, text) to service_role;
revoke all on function public.deerid_attach_media_to_profile_from_review(uuid, bigint, integer, uuid) from public, anon, authenticated;
grant execute on function public.deerid_attach_media_to_profile_from_review(uuid, bigint, integer, uuid) to service_role;
