-- Re-evaluate suppression after either side of the validation join changes.
-- A human label may predate its pinned prediction; that later prediction can expose a miss.
create or replace function deerid.gate1b_recheck_suppression()
returns trigger language plpgsql security definer
set search_path = pg_catalog, public, deerid
as $$
begin
  update deerid.gate1b_policy p set suppression_enabled = false
  where p.singleton and p.suppression_enabled
    and not deerid.gate1b_model_ready(
      p.model_name, p.model_version, p.minimum_labels,
      p.minimum_buck_events, p.minimum_whitetail_labels,
      p.minimum_axis_labels, p.minimum_whitetail_buck_events,
      p.minimum_axis_buck_events, p.required_buck_recall
    );
  return null;
end;
$$;

drop trigger if exists gate1b_label_rechecks_policy on deerid.gate1b_human_labels;
create trigger gate1b_label_rechecks_policy
after insert on deerid.gate1b_human_labels
for each statement execute function deerid.gate1b_recheck_suppression();

drop trigger if exists gate1b_prediction_rechecks_policy on deerid.gate1b_predictions;
create trigger gate1b_prediction_rechecks_policy
after insert on deerid.gate1b_predictions
for each statement execute function deerid.gate1b_recheck_suppression();

revoke all on function deerid.gate1b_recheck_suppression() from public, anon, authenticated;
drop function deerid.disable_gate1b_after_label_regression();
