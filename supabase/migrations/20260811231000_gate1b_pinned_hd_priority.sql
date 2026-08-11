-- Keep billable HD prioritization pinned to the same exact Gate 1B model as validation.
create or replace function deerid.assign_gate1b_hd_priority()
returns trigger language plpgsql
set search_path = pg_catalog, deerid, pg_temp
as $$
declare class text;
begin
  select gp.triage_class into class
  from deerid.gate1b_predictions gp
  cross join deerid.gate1b_policy policy
  where gp.media_id = new.media_id and policy.singleton
    and gp.model_name = policy.model_name and gp.model_version = policy.model_version
  order by gp.created_at desc, gp.id desc limit 1;
  if class = 'likely_male' then
    new.priority := 100; new.priority_reason := 'gate1b_likely_male';
  elsif class = 'female_candidate' then
    new.priority := 10; new.priority_reason := 'gate1b_female_candidate';
  else
    new.priority := 50; new.priority_reason := 'gate1b_uncertain_or_pending';
  end if;
  return new;
end;
$$;

revoke all on function deerid.assign_gate1b_hd_priority() from public, anon, authenticated;
