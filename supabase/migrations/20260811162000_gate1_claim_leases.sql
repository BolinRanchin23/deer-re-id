-- Atomically lease complete Gate 1 event batches across all worker paths.

create table deerid.gate1_claims (
  media_id uuid not null references deerid.media(id) on delete cascade,
  model_name text not null,
  model_version text not null,
  claim_token uuid not null,
  claimed_at timestamptz not null default now(),
  leased_until timestamptz not null,
  primary key (media_id, model_name, model_version)
);

create index gate1_claims_token_idx on deerid.gate1_claims(claim_token);
create index gate1_claims_lease_idx on deerid.gate1_claims(leased_until);
alter table deerid.gate1_claims enable row level security;

create or replace function public.deerid_gate1_pending(
  p_model_name text,
  p_model_version text,
  p_limit integer default 40
)
returns jsonb
language sql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with lock_guard as (
    select pg_advisory_xact_lock(hashtextextended(p_model_name || ':' || p_model_version, 0))
  ), expired as (
    delete from deerid.gate1_claims where leased_until <= now()
  ), ordered as (
    select
      m.id,
      m.camera_id,
      m.captured_at,
      m.object_path,
      coalesce(m.camera_id::text, 'media:' || m.id::text) as camera_group,
      lag(m.captured_at) over (
        partition by coalesce(m.camera_id::text, 'media:' || m.id::text)
        order by m.captured_at, m.id
      ) as previous_at
    from deerid.media m
    cross join lock_guard
    where m.variant = 'cloud_thumbnail'
      and m.captured_at is not null
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
      camera_group || ':' ||
      extract(epoch from event_start)::text || ':' || chunk_number::text
    ), 1, 24) as event_key
    from evented
  ), available as (
    select k.*
    from keyed k
    where not exists (
      select 1 from deerid.gate1_assessments a
      where a.media_id = k.id
        and a.model_name = p_model_name
        and a.model_version = p_model_version
    )
      and not exists (
        select 1
        from keyed event_member
        join deerid.gate1_claims c on c.media_id = event_member.id
        where event_member.event_key = k.event_key
          and c.model_name = p_model_name
          and c.model_version = p_model_version
          and c.leased_until > now()
      )
  ), candidate_events as (
    select a.event_key, min(a.event_start) as event_start
    from available a
    group by a.event_key
    order by min(a.event_start), a.event_key
    limit least(greatest(p_limit, 1), 50)
  ), claim_value as (
    select gen_random_uuid() as claim_token
  ), claimed as (
    insert into deerid.gate1_claims (
      media_id, model_name, model_version, claim_token, leased_until
    )
    select
      a.id, p_model_name, p_model_version, v.claim_token,
      now() + interval '25 minutes'
    from available a
    join candidate_events e on e.event_key = a.event_key
    cross join claim_value v
    on conflict (media_id, model_name, model_version) do nothing
    returning media_id, claim_token
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'media_id', a.id,
    'camera_id', a.camera_id,
    'captured_at', a.captured_at,
    'object_path', a.object_path,
    'event_key', a.event_key,
    'claim_token', c.claim_token
  ) order by a.captured_at, a.id), '[]'::jsonb)
  from available a
  join claimed c on c.media_id = a.id;
$$;

create or replace function public.deerid_release_gate1_claim(p_claim_token uuid)
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
  return jsonb_build_object('ok', true, 'released', released_count);
end;
$$;

revoke all on function public.deerid_gate1_pending(text, text, integer) from public;
revoke all on function public.deerid_gate1_pending(text, text, integer) from anon;
revoke all on function public.deerid_gate1_pending(text, text, integer) from authenticated;
grant execute on function public.deerid_gate1_pending(text, text, integer) to service_role;

revoke all on function public.deerid_release_gate1_claim(uuid) from public;
revoke all on function public.deerid_release_gate1_claim(uuid) from anon;
revoke all on function public.deerid_release_gate1_claim(uuid) from authenticated;
grant execute on function public.deerid_release_gate1_claim(uuid) to service_role;
