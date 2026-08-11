-- Fence Gate 1 writes with event-complete leases and consume claims atomically.

drop function public.deerid_gate1_pending(text, text, integer);
drop function public.deerid_release_gate1_claim(uuid);
drop table deerid.gate1_claims;

create table deerid.gate1_claims (
  event_key text not null,
  model_name text not null,
  model_version text not null,
  claim_token uuid not null,
  media_ids uuid[] not null check (cardinality(media_ids) between 1 and 10),
  claimed_at timestamptz not null default now(),
  leased_until timestamptz not null,
  primary key (event_key, model_name, model_version)
);

create index gate1_claims_token_idx on deerid.gate1_claims(claim_token);
create index gate1_claims_lease_idx on deerid.gate1_claims(leased_until);
alter table deerid.gate1_claims enable row level security;

create function public.deerid_gate1_pending(
  p_model_name text,
  p_model_version text,
  p_limit integer default 40
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  new_token uuid := gen_random_uuid();
  response jsonb;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_model_name || ':' || p_model_version, 0));
  delete from deerid.gate1_claims where leased_until <= now();

  with ordered as (
    select
      m.id, m.camera_id, m.captured_at, m.object_path,
      coalesce(m.camera_id::text, 'media:' || m.id::text) as camera_group,
      lag(m.captured_at) over (
        partition by coalesce(m.camera_id::text, 'media:' || m.id::text)
        order by m.captured_at, m.id
      ) as previous_at
    from deerid.media m
    where m.variant = 'cloud_thumbnail' and m.captured_at is not null
  ), marked as (
    select *, case
      when previous_at is null or captured_at - previous_at > interval '5 seconds' then 1
      else 0
    end as starts_event
    from ordered
  ), chained as (
    select *, sum(starts_event) over (
      partition by camera_group order by captured_at, id rows unbounded preceding
    ) as chain_number
    from marked
  ), chunked as (
    select *, ((row_number() over (
      partition by camera_group, chain_number order by captured_at, id
    ) - 1) / 10)::integer as chunk_number
    from chained
  ), evented as (
    select *, min(captured_at) over (
      partition by camera_group, chain_number, chunk_number
    ) as event_start
    from chunked
  ), keyed as (
    select *, substr(md5(
      camera_group || ':' || extract(epoch from event_start)::text || ':' || chunk_number::text
    ), 1, 24) as event_key
    from evented
  ), candidate_keys as (
    select k.event_key, min(k.event_start) as event_start
    from keyed k
    where exists (
      select 1
      from keyed member
      where member.event_key = k.event_key
        and not exists (
          select 1 from deerid.gate1_assessments a
          where a.media_id = member.id
            and a.model_name = p_model_name
            and a.model_version = p_model_version
        )
    )
      and not exists (
        select 1 from deerid.gate1_claims c
        where c.event_key = k.event_key
          and c.model_name = p_model_name
          and c.model_version = p_model_version
      )
    group by k.event_key
    order by min(k.event_start), k.event_key
    limit least(greatest(p_limit, 1), 50)
  ), candidate_events as (
    select c.event_key, c.event_start, array_agg(k.id order by k.captured_at, k.id) as media_ids
    from candidate_keys c
    join keyed k on k.event_key = c.event_key
    group by c.event_key, c.event_start
  ), claimed as (
    insert into deerid.gate1_claims (
      event_key, model_name, model_version, claim_token, media_ids, leased_until
    )
    select event_key, p_model_name, p_model_version, new_token, media_ids,
           now() + interval '25 minutes'
    from candidate_events
    returning event_key, claim_token, media_ids
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'media_id', k.id,
    'camera_id', k.camera_id,
    'captured_at', k.captured_at,
    'object_path', k.object_path,
    'event_key', k.event_key,
    'claim_token', c.claim_token
  ) order by k.captured_at, k.id), '[]'::jsonb)
  into response
  from keyed k
  join claimed c on k.id = any(c.media_ids);

  return response;
end;
$$;

-- Remove the unfenced writer so every production write must prove live ownership.
drop function public.deerid_record_gate1_batch(text, text, jsonb);

create function public.deerid_record_gate1_batch(
  p_model_name text,
  p_model_version text,
  p_claim_token uuid,
  p_results jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  item jsonb;
  inserted_count integer := 0;
  affected integer := 0;
  claim_event_count integer := 0;
  claim_media_count integer := 0;
  released_count integer := 0;
  item_route text;
  item_reason text;
  item_representative boolean;
begin
  if jsonb_typeof(p_results) <> 'array' or jsonb_array_length(p_results) > 500
     or length(p_model_name) not between 1 and 120
     or length(p_model_version) not between 1 and 120
     or p_claim_token is null then
    raise exception 'invalid fenced gate1 batch';
  end if;

  perform 1
  from deerid.gate1_claims c
  where c.claim_token = p_claim_token
    and c.model_name = p_model_name
    and c.model_version = p_model_version
    and c.leased_until > now()
  for update;

  select count(*), coalesce(sum(cardinality(c.media_ids)), 0)
  into claim_event_count, claim_media_count
  from deerid.gate1_claims c
  where c.claim_token = p_claim_token
    and c.model_name = p_model_name
    and c.model_version = p_model_version
    and c.leased_until > now();

  if claim_event_count = 0
     or claim_media_count <> jsonb_array_length(p_results)
     or claim_media_count <> (
       select count(distinct (value->>'media_id')::uuid)
       from jsonb_array_elements(p_results)
     )
     or exists (
       select 1
       from jsonb_array_elements(p_results) result
       where not exists (
         select 1 from deerid.gate1_claims c
         where c.claim_token = p_claim_token
           and c.model_name = p_model_name
           and c.model_version = p_model_version
           and c.leased_until > now()
           and (result.value->>'media_id')::uuid = any(c.media_ids)
           and result.value->>'event_key' = c.event_key
       )
     )
     or exists (
       select 1
       from deerid.gate1_claims c
       cross join lateral unnest(c.media_ids) as member(media_id)
       where c.claim_token = p_claim_token
         and c.model_name = p_model_name
         and c.model_version = p_model_version
         and c.leased_until > now()
         and not exists (
           select 1 from jsonb_array_elements(p_results) result
           where (result.value->>'media_id')::uuid = member.media_id
             and result.value->>'event_key' = c.event_key
         )
     ) then
    raise exception 'stale, incomplete, or mismatched gate1 claim';
  end if;

  for item in select value from jsonb_array_elements(p_results) loop
    item_route := coalesce(item->>'route', '');
    item_reason := coalesce(item->>'reason', 'unspecified');
    item_representative := coalesce((item->>'is_representative')::boolean, false);
    if item_route not in ('review', 'archive', 'event_duplicate')
       or length(coalesce(item->>'event_key', '')) not between 8 and 80 then
      raise exception 'invalid gate1 result';
    end if;
    if item_representative and exists (
      select 1 from deerid.gate1_assessments a
      where a.event_key = item->>'event_key'
        and a.model_name = p_model_name
        and a.model_version = p_model_version
        and a.is_representative
    ) then
      item_representative := false;
      item_route := 'event_duplicate';
      item_reason := 'existing_event_representative';
    end if;
    insert into deerid.gate1_assessments (
      media_id, event_key, route, reason, is_representative, model_name, model_version,
      animal_confidence, animal_area, species_label, species_confidence, detections, raw_output
    ) values (
      (item->>'media_id')::uuid, item->>'event_key', item_route, item_reason,
      item_representative, p_model_name, p_model_version,
      coalesce((item->>'animal_confidence')::double precision, 0),
      coalesce((item->>'animal_area')::double precision, 0), nullif(item->>'species_label', ''),
      coalesce((item->>'species_confidence')::double precision, 0),
      coalesce(item->'detections', '[]'::jsonb), coalesce(item->'raw_output', '{}'::jsonb)
    ) on conflict (media_id, model_name, model_version) do nothing;
    get diagnostics affected = row_count;
    inserted_count := inserted_count + affected;
  end loop;

  delete from deerid.gate1_claims where claim_token = p_claim_token;
  get diagnostics released_count = row_count;
  if released_count <> claim_event_count then
    raise exception 'gate1 claim consumption mismatch';
  end if;
  return jsonb_build_object(
    'ok', true, 'inserted', inserted_count, 'released', released_count
  );
end;
$$;

create function public.deerid_release_gate1_claim(p_claim_token uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  released_count integer;
begin
  delete from deerid.gate1_claims where claim_token = p_claim_token;
  get diagnostics released_count = row_count;
  return jsonb_build_object('ok', released_count > 0, 'released', released_count);
end;
$$;

create unique index if not exists gate1_one_representative_per_event_idx
on deerid.gate1_assessments(event_key, model_name, model_version)
where is_representative;

revoke all on function public.deerid_gate1_pending(text, text, integer) from public, anon, authenticated;
revoke all on function public.deerid_record_gate1_batch(text, text, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.deerid_release_gate1_claim(uuid) from public, anon, authenticated;
grant execute on function public.deerid_gate1_pending(text, text, integer) to service_role;
grant execute on function public.deerid_record_gate1_batch(text, text, uuid, jsonb) to service_role;
grant execute on function public.deerid_release_gate1_claim(uuid) to service_role;
