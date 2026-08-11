-- Explain archived Gate 1 outcomes in the live funnel.

create or replace function public.deerid_gate1_funnel(
  p_model_name text,
  p_model_version text
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
  with thumbnails as (
    select m.id
    from deerid.media m
    where m.variant = 'cloud_thumbnail'
  ), assessed as (
    select g.id, g.media_id, g.route, g.reason, g.is_representative
    from deerid.gate1_assessments g
    join thumbnails t on t.id = g.media_id
    where g.model_name = p_model_name
      and g.model_version = p_model_version
  ), review_counts as (
    select
      count(*) filter (
        where a.route = 'review' and a.is_representative
          and not coalesce(s.resolved, false)
      ) as unresolved_review,
      count(*) filter (
        where a.route = 'review' and a.is_representative
          and coalesce(s.resolved, false)
      ) as resolved_review
    from assessed a
    left join deerid.gate1_review_state s on s.gate1_assessment_id = a.id
  )
  select jsonb_build_object(
    'model_name', p_model_name,
    'model_version', p_model_version,
    'total_thumbnails', (select count(*) from thumbnails),
    'assessed_thumbnails', (select count(*) from assessed),
    'pending_thumbnails', (select count(*) from thumbnails) - (select count(*) from assessed),
    'review_representatives', (
      select count(*) from assessed where route = 'review' and is_representative
    ),
    'event_duplicates', (select count(*) from assessed where route = 'event_duplicate'),
    'archived', (select count(*) from assessed where route = 'archive'),
    'blank_or_below_threshold', (
      select count(*) from assessed
      where route = 'archive' and reason = 'blank_or_below_threshold'
    ),
    'confident_non_target', (
      select count(*) from assessed
      where route = 'archive' and reason = 'confident_non_target'
    ),
    'unresolved_review', (select unresolved_review from review_counts),
    'resolved_review', (select resolved_review from review_counts)
  );
$$;

revoke all on function public.deerid_gate1_funnel(text, text) from public;
revoke all on function public.deerid_gate1_funnel(text, text) from anon;
revoke all on function public.deerid_gate1_funnel(text, text) from authenticated;
grant execute on function public.deerid_gate1_funnel(text, text) to service_role;
