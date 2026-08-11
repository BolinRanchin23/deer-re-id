-- Submit human-selected HD requests to Reveal with retry-safe database fencing.

alter table deerid.gate1_review_state
  add column if not exists pending_hd boolean not null default false;

alter table deerid.hd_requests drop constraint if exists hd_requests_status_check;
alter table deerid.hd_requests
  add constraint hd_requests_status_check
  check (status in ('queued', 'requesting', 'submitted', 'available', 'failed', 'unknown', 'cancelled'));
alter table deerid.hd_requests
  add column if not exists request_token uuid,
  add column if not exists request_started_at timestamptz,
  add column if not exists submitted_at timestamptz,
  add column if not exists gate1_assessment_id bigint references deerid.gate1_assessments(id) on delete set null,
  add column if not exists review_version integer,
  add column if not exists pending_note text check (pending_note is null or length(pending_note) <= 500);
create unique index if not exists hd_requests_request_token_idx
  on deerid.hd_requests (request_token) where request_token is not null;

create or replace function public.deerid_record_review_decision(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_action text,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment_id bigint;
  decision_id bigint;
  advanced_id bigint;
begin
  if p_action not in ('request_hd', 'keep_for_identity', 'not_useful', 'defer')
     or length(coalesce(p_note, '')) > 500 or p_review_version < 0 then
    raise exception 'invalid review decision';
  end if;
  if p_action = 'request_hd' then
    raise exception 'HD requests require the fenced provider pipeline';
  end if;
  select id into latest_assessment_id from deerid.gate1_assessments
  where media_id = p_media_id and route = 'review' and is_representative
  order by created_at desc, id desc limit 1;
  if latest_assessment_id is distinct from p_assessment_id then
    raise exception 'stale review capability';
  end if;
  insert into deerid.gate1_review_state (gate1_assessment_id)
  values (p_assessment_id) on conflict do nothing;
  update deerid.gate1_review_state
  set version = version + 1, resolved = (p_action <> 'defer'), updated_at = now()
  where gate1_assessment_id = p_assessment_id and version = p_review_version
    and not resolved and not pending_hd
  returning gate1_assessment_id into advanced_id;
  if advanced_id is null then raise exception 'stale, resolved, or pending review capability'; end if;
  insert into deerid.review_decisions (media_id, gate1_assessment_id, review_version, action, note)
  values (p_media_id, p_assessment_id, p_review_version, p_action, nullif(trim(coalesce(p_note, '')), ''))
  returning id into decision_id;
  return jsonb_build_object('ok', true, 'media_id', p_media_id, 'action', p_action, 'decision_id', decision_id);
end;
$$;

create or replace function public.deerid_begin_hd_request(
  p_media_id uuid,
  p_assessment_id bigint,
  p_review_version integer,
  p_note text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  latest_assessment_id bigint;
  provider_id text;
  existing deerid.hd_requests%rowtype;
  new_token uuid := gen_random_uuid();
  reserved_id bigint;
begin
  if p_media_id is null or p_assessment_id is null or p_review_version < 0
     or length(coalesce(p_note, '')) > 500 then
    raise exception 'invalid HD request';
  end if;
  select a.id, m.provider_photo_id into latest_assessment_id, provider_id
  from deerid.gate1_assessments a join deerid.media m on m.id = a.media_id
  where a.media_id = p_media_id and a.route = 'review' and a.is_representative
  order by a.created_at desc, a.id desc limit 1;
  if latest_assessment_id is distinct from p_assessment_id or provider_id is null then
    raise exception 'stale HD request capability';
  end if;

  select * into existing from deerid.hd_requests where media_id = p_media_id for update;
  if existing.id is not null and existing.status in ('submitted', 'available') then
    return jsonb_build_object('ok', true, 'should_request', false, 'status', existing.status);
  end if;
  -- A request may have reached Reveal before this process stopped. Never resend an
  -- ambiguous external side effect; ingestion reconciles it when HD becomes visible.
  if existing.id is not null and existing.status in ('requesting', 'unknown') then
    return jsonb_build_object('ok', true, 'should_request', false, 'status', existing.status);
  end if;

  insert into deerid.gate1_review_state (gate1_assessment_id)
  values (p_assessment_id) on conflict do nothing;
  update deerid.gate1_review_state
  set pending_hd = true, updated_at = now()
  where gate1_assessment_id = p_assessment_id and version = p_review_version
    and not resolved and not pending_hd
  returning gate1_assessment_id into reserved_id;
  if reserved_id is null then raise exception 'stale, resolved, or pending review capability'; end if;

  insert into deerid.hd_requests (
    media_id, status, request_token, request_started_at, gate1_assessment_id,
    review_version, pending_note, attempts, last_error, updated_at
  ) values (
    p_media_id, 'requesting', new_token, now(), p_assessment_id,
    p_review_version, nullif(trim(coalesce(p_note, '')), ''),
    coalesce(existing.attempts, 0), null, now()
  )
  on conflict (media_id) do update set
    status = 'requesting', request_token = excluded.request_token,
    request_started_at = excluded.request_started_at,
    gate1_assessment_id = excluded.gate1_assessment_id,
    review_version = excluded.review_version, pending_note = excluded.pending_note,
    last_error = null, updated_at = now();

  return jsonb_build_object(
    'ok', true, 'should_request', true, 'status', 'requesting',
    'request_token', new_token, 'provider_photo_id', provider_id
  );
end;
$$;

create or replace function public.deerid_complete_hd_request(p_request_token uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  request_row deerid.hd_requests%rowtype;
  decision_id bigint;
  advanced_id bigint;
begin
  select * into request_row from deerid.hd_requests
  where request_token = p_request_token and status = 'requesting' for update;
  if request_row.id is null then raise exception 'stale HD request token'; end if;

  if request_row.requested_by_decision_id is null then
    update deerid.gate1_review_state
    set pending_hd = false, resolved = true, version = version + 1, updated_at = now()
    where gate1_assessment_id = request_row.gate1_assessment_id
      and version = request_row.review_version and not resolved and pending_hd
    returning gate1_assessment_id into advanced_id;
    if advanced_id is null then raise exception 'stale HD review capability'; end if;
    insert into deerid.review_decisions (
      media_id, gate1_assessment_id, review_version, action, note
    ) values (
      request_row.media_id, request_row.gate1_assessment_id,
      request_row.review_version, 'request_hd', request_row.pending_note
    ) returning id into decision_id;
  else
    decision_id := request_row.requested_by_decision_id;
  end if;

  update deerid.hd_requests set
    status = 'submitted', requested_by_decision_id = decision_id,
    attempts = attempts + 1, submitted_at = now(), updated_at = now(),
    request_token = null, request_started_at = null, last_error = null
  where id = request_row.id;
  return jsonb_build_object('ok', true, 'status', 'submitted', 'request_id', request_row.id);
end;
$$;

create or replace function public.deerid_fail_hd_request(
  p_request_token uuid,
  p_error_code text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  request_row deerid.hd_requests%rowtype;
begin
  if p_error_code not in ('provider_rejected', 'provider_unavailable', 'deadline') then
    raise exception 'invalid HD failure code';
  end if;
  select * into request_row from deerid.hd_requests
  where request_token = p_request_token and status = 'requesting' for update;
  if request_row.id is null then raise exception 'stale HD request token'; end if;
  if request_row.requested_by_decision_id is null then
    update deerid.gate1_review_state set pending_hd = false, updated_at = now()
    where gate1_assessment_id = request_row.gate1_assessment_id
      and version = request_row.review_version and pending_hd and not resolved;
  end if;
  update deerid.hd_requests set
    status = 'failed', attempts = attempts + 1, last_error = p_error_code,
    request_token = null, request_started_at = null, updated_at = now()
  where id = request_row.id;
  return jsonb_build_object('ok', true, 'status', 'failed');
end;
$$;

create or replace function public.deerid_mark_hd_request_unknown(
  p_request_token uuid,
  p_error_code text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  request_id bigint;
begin
  if p_error_code <> 'provider_outcome_unknown' then
    raise exception 'invalid HD unknown-outcome code';
  end if;
  update deerid.hd_requests set
    status = 'unknown', attempts = attempts + 1, last_error = p_error_code,
    request_token = null, request_started_at = null, updated_at = now()
  where request_token = p_request_token and status = 'requesting'
  returning id into request_id;
  if request_id is null then raise exception 'stale HD request token'; end if;
  -- Keep pending_hd set: a fresh capability must not create a duplicate request.
  return jsonb_build_object('ok', true, 'status', 'unknown');
end;
$$;

create or replace function public.deerid_claim_queued_hd_request()
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  queued_id bigint;
  new_token uuid := gen_random_uuid();
  provider_id text;
begin
  select h.id, m.provider_photo_id into queued_id, provider_id
  from deerid.hd_requests h join deerid.media m on m.id = h.media_id
  where h.status = 'queued'
  order by h.created_at, h.id
  for update of h skip locked
  limit 1;
  if queued_id is null then
    return jsonb_build_object('ok', true, 'empty', true);
  end if;
  update deerid.hd_requests set
    status = 'requesting', request_token = new_token,
    request_started_at = now(), updated_at = now(), last_error = null
  where id = queued_id;
  return jsonb_build_object(
    'ok', true, 'empty', false, 'request_token', new_token,
    'provider_photo_id', provider_id
  );
end;
$$;

create or replace function deerid.mark_hd_request_available()
returns trigger
language plpgsql
set search_path = pg_catalog, deerid, pg_temp
as $$
declare
  became_available boolean := false;
  request_row deerid.hd_requests%rowtype;
  advanced_id bigint;
  decision_id bigint;
begin
  if new.hd_photo is true then
    if tg_op = 'INSERT' then
      became_available := true;
    elsif old.hd_photo is distinct from true then
      became_available := true;
    end if;
  end if;
  if not became_available then return new; end if;

  select * into request_row from deerid.hd_requests
  where media_id = new.id for update;
  if request_row.id is null then return new; end if;

  decision_id := request_row.requested_by_decision_id;
  if decision_id is null and request_row.gate1_assessment_id is not null then
    update deerid.gate1_review_state
    set pending_hd = false, resolved = true, version = version + 1, updated_at = now()
    where gate1_assessment_id = request_row.gate1_assessment_id
      and version = request_row.review_version and not resolved and pending_hd
    returning gate1_assessment_id into advanced_id;
    if advanced_id is not null then
      insert into deerid.review_decisions (
        media_id, gate1_assessment_id, review_version, action, note
      ) values (
        request_row.media_id, request_row.gate1_assessment_id,
        request_row.review_version, 'request_hd', request_row.pending_note
      ) returning id into decision_id;
    end if;
  end if;

  update deerid.hd_requests set
    status = 'available', requested_by_decision_id = coalesce(decision_id, requested_by_decision_id),
    updated_at = now(), last_error = null, request_token = null, request_started_at = null
  where id = request_row.id
    and status in ('queued', 'requesting', 'submitted', 'failed', 'unknown');
  return new;
end;
$$;

drop trigger if exists media_hd_request_available on deerid.media;
create trigger media_hd_request_available
after insert or update of hd_photo on deerid.media
for each row execute function deerid.mark_hd_request_available();

revoke all on function deerid.mark_hd_request_available() from public, anon, authenticated;

create or replace function public.deerid_private_library(p_limit integer default 60)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  select coalesce(jsonb_agg(to_jsonb(feed) - 'queue_priority' order by feed.captured_at desc), '[]'::jsonb)
  from (
    select
      m.id, m.captured_at, m.camera_id, c.name as camera_name, m.variant,
      m.width, m.height, m.hd_photo, m.has_headshot, m.battery_level, m.signal_level,
      coalesce((select jsonb_agg(jsonb_build_object(
        'namespace', l.namespace, 'label', l.label, 'source', l.source,
        'confidence', l.confidence, 'status', l.status) order by l.created_at)
        from deerid.media_labels l where l.media_id = m.id), '[]'::jsonb) as labels,
      coalesce((select jsonb_agg(jsonb_build_object(
        'animal_id', a.id, 'profile_id', ap.id, 'display_name', a.display_name,
        'season_year', ap.season_year, 'confirmation_status', am.confirmation_status,
        'match_confidence', am.match_confidence) order by a.display_name)
        from deerid.animal_media am
        join deerid.animal_profiles ap on ap.id = am.animal_profile_id
        join deerid.animals a on a.id = ap.animal_id
        where am.media_id = m.id), '[]'::jsonb) as animals,
      case when g.id is null then null else jsonb_build_object(
        'id', g.id, 'event_key', g.event_key, 'route', g.route, 'reason', g.reason,
        'is_representative', g.is_representative, 'model_name', g.model_name,
        'model_version', g.model_version, 'animal_confidence', g.animal_confidence,
        'animal_area', g.animal_area, 'species_label', g.species_label,
        'species_confidence', g.species_confidence, 'review_version', coalesce(s.version, 0),
        'pending_hd', coalesce(s.pending_hd, false), 'created_at', g.created_at) end as gate1,
      case when r.id is null then null else jsonb_build_object(
        'action', r.action, 'note', r.note, 'decided_at', r.decided_at) end as review_decision,
      case when g.route = 'review' and g.is_representative
        and not coalesce(s.resolved, false) and not coalesce(s.pending_hd, false)
        then 0 else 1 end as queue_priority
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    left join lateral (
      select * from deerid.gate1_assessments ga where ga.media_id = m.id
      order by ga.created_at desc, ga.id desc limit 1
    ) g on true
    left join deerid.gate1_review_state s on s.gate1_assessment_id = g.id
    left join lateral (
      select * from deerid.review_decisions rd where rd.gate1_assessment_id = g.id
      order by rd.decided_at desc, rd.id desc limit 1
    ) r on true
    order by queue_priority, m.captured_at desc
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  ) feed;
$$;

revoke all on function public.deerid_begin_hd_request(uuid, bigint, integer, text) from public, anon, authenticated;
revoke all on function public.deerid_complete_hd_request(uuid) from public, anon, authenticated;
revoke all on function public.deerid_fail_hd_request(uuid, text) from public, anon, authenticated;
revoke all on function public.deerid_mark_hd_request_unknown(uuid, text) from public, anon, authenticated;
revoke all on function public.deerid_claim_queued_hd_request() from public, anon, authenticated;
grant execute on function public.deerid_begin_hd_request(uuid, bigint, integer, text) to service_role;
grant execute on function public.deerid_complete_hd_request(uuid) to service_role;
grant execute on function public.deerid_fail_hd_request(uuid, text) to service_role;
grant execute on function public.deerid_mark_hd_request_unknown(uuid, text) to service_role;
grant execute on function public.deerid_claim_queued_hd_request() to service_role;
