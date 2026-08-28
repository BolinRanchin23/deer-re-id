-- Immutable safety floors prevent configuration drift from weakening suppression validation.
alter table deerid.gate1b_policy
  add constraint gate1b_policy_validation_safety_floor check (
    minimum_labels >= 100
    and minimum_buck_events >= 20
    and minimum_whitetail_labels >= 10
    and minimum_axis_labels >= 10
    and minimum_whitetail_buck_events >= 10
    and minimum_axis_buck_events >= 5
    and required_buck_recall >= 0.99
    and female_audit_percent >= 10
  );
