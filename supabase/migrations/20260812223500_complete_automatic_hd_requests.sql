-- Complete machine-created HD requests without requiring a human review-state
-- capability. Human-created requests retain the original fenced decision path.
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

  if request_row.review_version is null
     and request_row.priority_reason in ('gate1b_automatic_likely_male','gate1b_audit_correction') then
    -- Automation has its own append-only gate1b_automation_events ledger.
    -- Do not fabricate a human review decision.
    decision_id := request_row.requested_by_decision_id;
  elsif request_row.requested_by_decision_id is null then
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
revoke all on function public.deerid_complete_hd_request(uuid) from public, anon, authenticated;
grant execute on function public.deerid_complete_hd_request(uuid) to service_role;

-- The first repaired drain reached Reveal before the old completion function
-- rejected its database transition. Preserve that ambiguous external outcome and
-- prohibit an automatic resend. Reveal ingestion will reconcile it to available.
update deerid.hd_requests
set status = 'unknown', attempts = attempts + 1,
    last_error = 'provider_outcome_unknown', request_token = null,
    request_started_at = null, updated_at = now()
where status = 'requesting'
  and review_version is null
  and priority_reason in ('gate1b_automatic_likely_male','gate1b_audit_correction');
