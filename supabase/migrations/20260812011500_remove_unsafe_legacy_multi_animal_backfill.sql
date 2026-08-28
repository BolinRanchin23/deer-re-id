-- A legacy result that reported multiple animals has no per-animal boxes.
-- Never expose its whole frame as one synthetic identity-bearing animal.
-- Keep reviewed historical rows for audit integrity; remove only unresolved
-- synthetic rows so they can be routed to a future box-correction workflow.

drop trigger hd_animal_instances_append_only on deerid.hd_animal_instances;

delete from deerid.hd_animal_instances i
using deerid.hd_review_results r
where i.hd_review_result_id = r.id
  and i.crop_recipe->>'source' = 'legacy_whole_frame'
  and coalesce((r.result->>'animal_count')::integer, 1) > 1
  and not exists (
    select 1
    from deerid.hd_review_decisions d
    where d.hd_animal_instance_id = i.id
  );

create trigger hd_animal_instances_append_only
before update or delete on deerid.hd_animal_instances
for each row execute function deerid.reject_hd_animal_instance_mutation();
