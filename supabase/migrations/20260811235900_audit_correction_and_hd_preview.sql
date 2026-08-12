-- Follow-up for live audit corrections and HD-asset preview resolution.

create or replace function public.deerid_record_gate1b_automation_label(
  p_automation_event_id bigint, p_verdict text, p_note text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
declare previous_id bigint; new_id bigint; action_name text;
begin
  if p_verdict not in ('correct','should_have_requested_hd','incorrect_male_or_antler') or length(coalesce(p_note,'')) > 500 then raise exception 'invalid automation label'; end if;
  select action into action_name from deerid.gate1b_automation_events where id=p_automation_event_id;
  if action_name is null then raise exception 'automation event not found'; end if;
  if (action_name='auto_suppress_female' and p_verdict='incorrect_male_or_antler') or
     (action_name='auto_request_hd' and p_verdict='should_have_requested_hd') then raise exception 'verdict does not match automation action'; end if;
  select id into previous_id from deerid.gate1b_automation_labels where automation_event_id=p_automation_event_id order by created_at desc,id desc limit 1;
  insert into deerid.gate1b_automation_labels(automation_event_id,supersedes_id,verdict,note)
  values(p_automation_event_id,previous_id,p_verdict,nullif(trim(coalesce(p_note,'')),'')) returning id into new_id;
  if p_verdict='should_have_requested_hd' then
    insert into deerid.hd_requests(media_id, gate1_assessment_id, priority, priority_reason)
    select e.media_id,e.gate1_assessment_id,100,'gate1b_audit_correction'
    from deerid.gate1b_automation_events e where e.id=p_automation_event_id
    on conflict(media_id) do nothing;
  end if;
  return jsonb_build_object('ok',true,'label_id',new_id);
end $$;
revoke all on function public.deerid_record_gate1b_automation_label(bigint,text,text) from public, anon, authenticated;
grant execute on function public.deerid_record_gate1b_automation_label(bigint,text,text) to service_role;

create or replace function public.deerid_gate1b_automation_audit(p_limit integer default 120)
returns jsonb language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp as $$
with latest_label as (
  select distinct on (automation_event_id) * from deerid.gate1b_automation_labels
  order by automation_event_id, created_at desc, id desc
)
select coalesce(jsonb_agg(jsonb_build_object(
  'automation_event_id', e.id, 'media_id', e.media_id, 'action', e.action,
  'policy_version', e.policy_version, 'model_name', e.model_name, 'model_version', e.model_version,
  'created_at', e.created_at, 'captured_at', m.captured_at, 'camera_name', c.name,
  'prediction', jsonb_build_object('species_label', p.species_label, 'visible_antler', p.visible_antler,
    'probable_male', p.probable_male, 'head_visibility', p.head_visibility,
    'triage_class', p.triage_class, 'reason', p.reason),
  'human_verdict', l.verdict, 'human_note', l.note, 'human_labeled_at', l.created_at
) order by e.created_at desc), '[]'::jsonb)
from (select * from deerid.gate1b_automation_events order by created_at desc limit greatest(1, least(coalesce(p_limit,120),500))) e
join deerid.media m on m.id=e.media_id left join deerid.cameras c on c.id=m.camera_id
join deerid.gate1b_predictions p on p.id=e.prediction_id left join latest_label l on l.automation_event_id=e.id;
$$;
revoke all on function public.deerid_gate1b_automation_audit(integer) from public, anon, authenticated;
grant execute on function public.deerid_gate1b_automation_audit(integer) to service_role;

create or replace function public.deerid_resolve_media_asset_object(p_media_asset_id uuid)
returns jsonb language sql stable security definer set search_path=pg_catalog,public,deerid,pg_temp as $$
select jsonb_build_object('object_path',object_path,'content_type',content_type) from deerid.media_assets where id=p_media_asset_id;
$$;
revoke all on function public.deerid_resolve_media_asset_object(uuid) from public,anon,authenticated;
grant execute on function public.deerid_resolve_media_asset_object(uuid) to service_role;
