-- Persist event membership so late media cannot rewrite event identity or overlap claims.

create table deerid.gate1_event_memberships (
  media_id uuid not null references deerid.media(id) on delete cascade,
  event_key text not null check (length(event_key) between 8 and 80),
  camera_id uuid,
  captured_at timestamptz not null,
  assigned_at timestamptz not null default now(),
  primary key (media_id)
);

create index gate1_event_memberships_event_idx
on deerid.gate1_event_memberships(event_key, captured_at, media_id);
create index gate1_event_memberships_camera_time_idx
on deerid.gate1_event_memberships(camera_id, captured_at);
alter table deerid.gate1_event_memberships enable row level security;

-- Preserve the event identities already used by durable assessments.
insert into deerid.gate1_event_memberships (media_id, event_key, camera_id, captured_at)
select distinct on (a.media_id)
  a.media_id, a.event_key, m.camera_id, m.captured_at
from deerid.gate1_assessments a
join deerid.media m on m.id = a.media_id
where m.variant = 'cloud_thumbnail' and m.captured_at is not null
order by a.media_id, a.created_at, a.id;

create function public.deerid_assign_gate1_events()
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  photo record;
  target_event_key text;
  assigned_count integer := 0;
begin
  perform pg_advisory_xact_lock(hashtextextended('deerid:gate1:event-topology', 0));

  for photo in
    select m.id, m.camera_id, m.captured_at
    from deerid.media m
    where m.variant = 'cloud_thumbnail'
      and m.captured_at is not null
      and not exists (
        select 1 from deerid.gate1_event_memberships membership
        where membership.media_id = m.id
      )
    order by m.captured_at, m.id
  loop
    target_event_key := null;
    if photo.camera_id is not null then
      select em.event_key
      into target_event_key
      from deerid.gate1_event_memberships em
      where em.camera_id = photo.camera_id
      group by em.event_key
      having count(*) < 10
         and min(abs(extract(epoch from (em.captured_at - photo.captured_at)))) <= 5
      order by
        min(abs(extract(epoch from (em.captured_at - photo.captured_at)))),
        min(em.captured_at),
        em.event_key
      limit 1;
    end if;

    if target_event_key is null then
      target_event_key := substr(md5(
        coalesce(photo.camera_id::text, 'media:' || photo.id::text) || ':' ||
        extract(epoch from photo.captured_at)::text || ':' || photo.id::text
      ), 1, 24);
    end if;

    insert into deerid.gate1_event_memberships (
      media_id, event_key, camera_id, captured_at
    ) values (
      photo.id, target_event_key, photo.camera_id, photo.captured_at
    ) on conflict (media_id) do nothing;
    if found then assigned_count := assigned_count + 1; end if;
  end loop;

  return assigned_count;
end;
$$;

-- The first assignment is part of the migration; later claims assign only new media.
select public.deerid_assign_gate1_events();
truncate table deerid.gate1_claims;

create or replace function public.deerid_gate1_pending(
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
  perform pg_advisory_xact_lock(hashtextextended('deerid:gate1:event-topology', 0));
  perform public.deerid_assign_gate1_events();
  delete from deerid.gate1_claims where leased_until <= now();

  with candidate_events as (
    select em.event_key, min(em.captured_at) as event_start,
           array_agg(em.media_id order by em.captured_at, em.media_id) as media_ids
    from deerid.gate1_event_memberships em
    where exists (
      select 1
      from deerid.gate1_event_memberships member
      where member.event_key = em.event_key
        and not exists (
          select 1 from deerid.gate1_assessments a
          where a.media_id = member.media_id
            and a.model_name = p_model_name
            and a.model_version = p_model_version
        )
    )
      and not exists (
        select 1 from deerid.gate1_claims c
        where c.event_key = em.event_key
          and c.model_name = p_model_name
          and c.model_version = p_model_version
      )
    group by em.event_key
    order by min(em.captured_at), em.event_key
    limit least(greatest(p_limit, 1), 50)
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
    'media_id', em.media_id,
    'camera_id', em.camera_id,
    'captured_at', em.captured_at,
    'object_path', m.object_path,
    'event_key', em.event_key,
    'claim_token', c.claim_token
  ) order by em.captured_at, em.media_id), '[]'::jsonb)
  into response
  from deerid.gate1_event_memberships em
  join claimed c on em.media_id = any(c.media_ids)
  join deerid.media m on m.id = em.media_id;

  return response;
end;
$$;

revoke all on function public.deerid_assign_gate1_events() from public, anon, authenticated;
grant execute on function public.deerid_assign_gate1_events() to service_role;
revoke all on function public.deerid_gate1_pending(text, text, integer) from public, anon, authenticated;
grant execute on function public.deerid_gate1_pending(text, text, integer) to service_role;
