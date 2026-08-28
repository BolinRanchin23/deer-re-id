-- Fair-share the bounded payload across all three unresolved Gate 1B queues.
create or replace function public.deerid_private_library(p_limit integer default 60)
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with policy as (select * from deerid.gate1b_policy where singleton), enriched as (
    select m.*, c.name as camera_name, g.id as gate1_id, g.event_key, g.route, g.reason as gate1_reason,
      g.is_representative, g.model_name as gate1_model_name, g.model_version as gate1_model_version,
      g.animal_confidence, g.animal_area, g.species_label as gate1_species,
      g.species_confidence, g.created_at as gate1_created_at,
      coalesce(s.version, 0) as review_version, coalesce(s.pending_hd, false) as pending_hd,
      coalesce(s.resolved, false) as resolved,
      r.action as review_action, r.note as review_note, r.decided_at,
      p.id as prediction_id, p.model_name as gate1b_model_name, p.model_version as gate1b_model_version,
      p.species_label as predicted_species, p.visible_antler as predicted_antler,
      p.probable_male as predicted_male, p.head_visibility as predicted_head,
      p.lighting, p.animal_count, p.mixed_group, p.all_animals_assessed,
      p.triage_class as predicted_triage, p.hd_recommended, p.model_failure,
      p.reason as gate1b_reason, p.created_at as gate1b_created_at,
      h.id as human_label_id, h.species_label as human_species,
      h.visible_antler as human_antler, h.probable_male as human_male,
      h.head_visibility as human_head, h.created_at as human_labeled_at,
      policy.suppression_enabled, policy.female_audit_percent
    from deerid.media m
    left join deerid.cameras c on c.id = m.camera_id
    left join lateral (select * from deerid.gate1_assessments ga where ga.media_id = m.id
      order by ga.created_at desc, ga.id desc limit 1) g on true
    left join deerid.gate1_review_state s on s.gate1_assessment_id = g.id
    left join lateral (select * from deerid.review_decisions rd where rd.gate1_assessment_id = g.id
      order by rd.decided_at desc, rd.id desc limit 1) r on true
    left join lateral (select * from deerid.gate1b_predictions gp where gp.gate1_assessment_id = g.id
      order by gp.created_at desc, gp.id desc limit 1) p on true
    left join lateral (select * from deerid.gate1b_human_labels gh where gh.gate1_assessment_id = g.id
      order by gh.created_at desc, gh.id desc limit 1) h on true
    cross join policy
  ), classified as (
    select e.*, case
      when human_label_id is not null and (human_antler = 'yes' or human_male = 'yes') then 'likely_male'
      when human_label_id is not null and human_species in ('whitetail','axis')
        and human_antler = 'no' and human_male = 'no' and human_head = 'full' then 'female_candidate'
      when human_label_id is not null then 'uncertain'
      else coalesce(predicted_triage, 'uncertain') end as effective_triage
    from enriched e
  ), routed as (
    select x.*, case
      when effective_triage = 'likely_male' then 'likely_male'
      when effective_triage = 'female_candidate'
        and mod(abs(hashtextextended(event_key, 0)), 100) < female_audit_percent then 'female_audit'
      when effective_triage = 'female_candidate' and suppression_enabled then 'suppressed'
      else 'uncertain' end as gate1b_queue
    from classified x
  ), candidates as (
    select
      id, captured_at, camera_id, camera_name, variant, width, height, hd_photo, has_headshot,
      battery_level, signal_level, gate1b_queue,
      coalesce((select jsonb_agg(jsonb_build_object('namespace', l.namespace, 'label', l.label,
        'source', l.source, 'confidence', l.confidence, 'status', l.status) order by l.created_at)
        from deerid.media_labels l where l.media_id = routed.id), '[]'::jsonb) as labels,
      coalesce((select jsonb_agg(jsonb_build_object('animal_id', a.id, 'profile_id', ap.id,
        'display_name', a.display_name, 'season_year', ap.season_year,
        'confirmation_status', am.confirmation_status, 'match_confidence', am.match_confidence)
        order by a.display_name) from deerid.animal_media am
        join deerid.animal_profiles ap on ap.id = am.animal_profile_id
        join deerid.animals a on a.id = ap.animal_id where am.media_id = routed.id), '[]'::jsonb) as animals,
      case when gate1_id is null then null else jsonb_build_object(
        'id', gate1_id, 'event_key', event_key, 'route', route, 'reason', gate1_reason,
        'is_representative', is_representative, 'model_name', gate1_model_name,
        'model_version', gate1_model_version, 'animal_confidence', animal_confidence,
        'animal_area', animal_area, 'species_label', gate1_species,
        'species_confidence', species_confidence, 'review_version', review_version,
        'pending_hd', pending_hd, 'created_at', gate1_created_at) end as gate1,
      case when prediction_id is null and human_label_id is null then null else jsonb_build_object(
        'prediction_id', prediction_id, 'model_name', gate1b_model_name,
        'model_version', gate1b_model_version, 'species_label', predicted_species,
        'visible_antler', predicted_antler, 'probable_male', predicted_male,
        'head_visibility', predicted_head, 'lighting', lighting, 'animal_count', animal_count,
        'mixed_group', mixed_group, 'all_animals_assessed', all_animals_assessed,
        'triage_class', effective_triage, 'queue', gate1b_queue,
        'hd_recommended', coalesce(hd_recommended, false), 'model_failure', coalesce(model_failure, false),
        'reason', gate1b_reason, 'created_at', gate1b_created_at,
        'human_label', case when human_label_id is null then null else jsonb_build_object(
          'id', human_label_id, 'species_label', human_species, 'visible_antler', human_antler,
          'probable_male', human_male, 'head_visibility', human_head,
          'created_at', human_labeled_at) end) end as gate1b,
      case when review_action is null then null else jsonb_build_object(
        'action', review_action, 'note', review_note, 'decided_at', decided_at) end as review_decision,
      case when route = 'review' and is_representative and not resolved and not pending_hd
        and gate1b_queue <> 'suppressed' then case gate1b_queue when 'likely_male' then 0
          when 'uncertain' then 1 when 'female_audit' then 2 else 3 end else 9 end as queue_priority
    from routed
  ),
  ranked as (
    select candidates.*,
      case when gate1 ->> 'route' = 'review' and review_decision is null
        and gate1b_queue <> 'suppressed' then 0 else 1 end as review_priority,
      row_number() over (
        partition by case when gate1 ->> 'route' = 'review' and review_decision is null
          and gate1b_queue <> 'suppressed' then gate1b_queue else 'non_review' end
        order by captured_at desc, id
      ) as queue_row
    from candidates
  ),
  feed as (
    select * from ranked
    order by review_priority, queue_row, queue_priority, captured_at desc, id
    limit greatest(1, least(coalesce(p_limit, 60), 60))
  )
  select coalesce(jsonb_agg(
    to_jsonb(feed) - 'queue_priority' - 'review_priority' - 'queue_row' - 'gate1b_queue'
    order by review_priority, queue_row, queue_priority, captured_at desc
  ), '[]'::jsonb)
  from feed;
$$;


revoke all on function public.deerid_private_library(integer) from public, anon, authenticated;
grant execute on function public.deerid_private_library(integer) to service_role;
