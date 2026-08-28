-- Align the operational HD automation trigger with the exact Gate 1B model
-- currently producing authoritative predictions. Keep female suppression disabled
-- until this model has local validation; only likely-male predictions auto-request HD.
update deerid.gate1b_policy
set model_name = 'OpenAI-GPT-4o-mini-Vision',
    model_version = 'gpt-4o-mini-2024-07-18@prompt-2026-08-12.1',
    automatic_hd_enabled = true,
    suppression_enabled = false,
    operating_mode = 'model_operational',
    policy_version = 'gate1b-openai-operational-hd-2026-08-12.1',
    updated_at = now()
where singleton;

-- The trigger only runs on insert. Replay existing predictions through the
-- idempotent policy function so male events missed during the model mismatch
-- receive durable HD queue records. Existing requests remain unchanged.
do $$
declare prediction_id bigint;
begin
  for prediction_id in
    select p.id
    from deerid.gate1b_predictions p
    where p.model_name = 'OpenAI-GPT-4o-mini-Vision'
      and p.model_version = 'gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'
      and p.triage_class = 'likely_male'
    order by p.id
  loop
    perform deerid.apply_gate1b_automation_prediction(prediction_id);
  end loop;
end $$;
