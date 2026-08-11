-- Keep Gate 1 bounded to cloud thumbnails with deterministic event keys.

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
  with ordered as (
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
  ), candidate_events as (
    select k.event_key, min(k.event_start) as event_start
    from keyed k
    where not exists (
      select 1 from deerid.gate1_assessments a
      where a.media_id = k.id
        and a.model_name = p_model_name
        and a.model_version = p_model_version
    )
    group by k.event_key
    order by min(k.event_start), k.event_key
    limit least(greatest(p_limit, 1), 50)
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'media_id', k.id,
    'camera_id', k.camera_id,
    'captured_at', k.captured_at,
    'object_path', k.object_path,
    'event_key', k.event_key
  ) order by k.captured_at, k.id), '[]'::jsonb)
  from keyed k
  join candidate_events c on c.event_key = k.event_key;
$$;

revoke all on function public.deerid_gate1_pending(text, text, integer) from public;
revoke all on function public.deerid_gate1_pending(text, text, integer) from anon;
revoke all on function public.deerid_gate1_pending(text, text, integer) from authenticated;
grant execute on function public.deerid_gate1_pending(text, text, integer) to service_role;
