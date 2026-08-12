import unittest
from pathlib import Path


class Gate1SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrations = sorted(Path("supabase/migrations").glob("*_gate1_review.sql"))
        if not migrations:
            raise AssertionError("Gate 1 migration is missing")
        cls.sql = migrations[-1].read_text()

    def test_append_only_evidence_and_human_action_tables_exist(self):
        for table in ("gate1_assessments", "review_decisions", "hd_requests"):
            self.assertIn(f"create table deerid.{table}", self.sql)
        self.assertIn("enable row level security", self.sql)

    def test_service_role_rpcs_cover_worker_review_and_library(self):
        for function in (
            "deerid_gate1_pending",
            "deerid_record_gate1_batch",
            "deerid_record_review_decision",
            "deerid_private_library",
        ):
            self.assertIn(f"function public.{function}", self.sql)
        self.assertIn("grant execute", self.sql)
        self.assertIn("service_role", self.sql)

    def test_request_hd_action_is_idempotently_queued(self):
        self.assertIn("request_hd", self.sql)
        self.assertIn("on conflict (media_id) do nothing", self.sql.lower())

    def test_hardening_migration_claims_stable_events_and_one_time_reviews(self):
        hardening = (
            sorted(Path("supabase/migrations").glob("*_gate1_hardening.sql"))[-1]
            .read_text()
            .lower()
        )
        self.assertIn("gate1_assessment_model_once_idx", hardening)
        self.assertIn("gate1_review_state", hardening)
        self.assertIn("p_review_version", hardening)
        self.assertIn("stale or resolved review capability", hardening)
        self.assertIn("event_start", hardening)
        self.assertIn("candidate_events", hardening)
        self.assertIn("queue_priority", hardening)

    def test_pending_hardening_filters_variants_and_bounds_complete_events(self):
        sql = (
            Path("supabase/migrations/20260811031500_gate1_pending_hardening.sql")
            .read_text()
            .lower()
        )
        self.assertIn("m.variant = 'cloud_thumbnail'", sql)
        self.assertIn("extract(epoch from event_start)", sql)
        self.assertIn("/ 10", sql)
        self.assertIn("limit least(greatest(p_limit, 1), 50)", sql)

    def test_pipeline_funnel_rpc_counts_each_gate_for_one_model_version(self):
        sql = (
            Path("supabase/migrations/20260811153000_gate1_funnel.sql")
            .read_text()
            .lower()
        )
        self.assertIn("function public.deerid_gate1_funnel", sql)
        for field in (
            "total_thumbnails",
            "assessed_thumbnails",
            "pending_thumbnails",
            "review_representatives",
            "event_duplicates",
            "archived",
            "unresolved_review",
            "resolved_review",
        ):
            self.assertIn(field, sql)
        self.assertIn("m.variant = 'cloud_thumbnail'", sql)
        self.assertIn("g.model_name = p_model_name", sql)
        self.assertIn("g.model_version = p_model_version", sql)
        self.assertIn("grant execute", sql)
        self.assertIn("service_role", sql)

    def test_pipeline_funnel_breaks_archives_into_blank_and_non_target_reasons(self):
        sql = (
            Path("supabase/migrations/20260811154500_gate1_funnel_reasons.sql")
            .read_text()
            .lower()
        )
        self.assertIn("blank_or_below_threshold", sql)
        self.assertIn("confident_non_target", sql)
        self.assertIn("reason", sql)

    def test_gate1_claims_are_atomic_leased_and_released(self):
        sql = (
            Path("supabase/migrations/20260811162000_gate1_claim_leases.sql")
            .read_text()
            .lower()
        )
        self.assertIn("create table deerid.gate1_claims", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("leased_until", sql)
        self.assertIn("claim_token", sql)
        self.assertIn("deerid_release_gate1_claim", sql)
        self.assertIn("grant execute", sql)
        self.assertIn("service_role", sql)

    def test_gate1_recording_is_fenced_and_claims_complete_events(self):
        sql = (
            Path("supabase/migrations/20260811163500_gate1_claim_fencing.sql")
            .read_text()
            .lower()
        )
        self.assertIn("media_ids uuid[]", sql)
        self.assertIn("p_claim_token uuid", sql)
        self.assertIn("for update", sql)
        self.assertIn("delete from deerid.gate1_claims", sql)
        self.assertIn("cardinality", sql)
        self.assertIn("jsonb_array_length", sql)

    def test_hd_requests_are_provider_submitted_with_fenced_retry_state(self):
        sql = (
            Path("supabase/migrations/20260811190000_hd_request_pipeline.sql")
            .read_text()
            .lower()
        )
        for function in (
            "deerid_begin_hd_request",
            "deerid_complete_hd_request",
            "deerid_fail_hd_request",
            "deerid_mark_hd_request_unknown",
        ):
            self.assertIn(f"function public.{function}", sql)
        self.assertIn("pending_hd", sql)
        self.assertIn("request_token", sql)
        self.assertIn("status = 'submitted'", sql)
        self.assertIn("for update", sql)
        self.assertIn("stale hd request token", sql)
        self.assertIn("media_hd_request_available", sql)
        self.assertIn("status = 'available'", sql)
        self.assertIn("'pending_hd', coalesce(s.pending_hd, false)", sql)
        self.assertIn("and not coalesce(s.pending_hd, false)", sql)
        self.assertIn("provider_outcome_unknown", sql)
        self.assertNotIn("interval '2 minutes'", sql)
        self.assertIn("service_role", sql)

    def test_gate1_event_membership_is_persisted_before_claiming(self):
        sql = (
            Path("supabase/migrations/20260811165000_gate1_stable_events.sql")
            .read_text()
            .lower()
        )
        self.assertIn("create table deerid.gate1_event_memberships", sql)
        self.assertIn("primary key (media_id)", sql)
        self.assertIn("deerid_assign_gate1_events", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("count(*) < 10", sql)
        self.assertIn("gate1_event_memberships em", sql)

    def test_gate1b_evidence_is_append_only_versioned_and_fails_closed(self):
        sql = (
            Path("supabase/migrations/20260811220000_gate1b_triage.sql")
            .read_text()
            .lower()
        )
        self.assertIn("create table deerid.gate1b_predictions", sql)
        self.assertIn("unique (gate1_assessment_id, model_name, model_version)", sql)
        self.assertIn("create table deerid.gate1b_human_labels", sql)
        self.assertIn("supersedes_id bigint references deerid.gate1b_human_labels", sql)
        self.assertIn("gate 1b evidence is append-only", sql)
        self.assertIn(
            "revoke update, delete, truncate on deerid.gate1b_predictions", sql
        )
        self.assertIn("unsafe female candidate", sql)

    def test_gate1b_suppression_stays_locked_until_recall_and_coverage_gate(self):
        sql = (
            Path("supabase/migrations/20260811220000_gate1b_triage.sql")
            .read_text()
            .lower()
        )
        self.assertIn("suppression_enabled boolean not null default false", sql)
        self.assertIn("minimum_labels integer not null default 100", sql)
        self.assertIn("minimum_buck_events integer not null default 20", sql)
        self.assertIn(
            "required_buck_recall double precision not null default 0.99", sql
        )
        self.assertIn("t.labeled_cameras >= 4", sql)
        self.assertIn("t.labeled_day > 0 and t.labeled_ir > 0", sql)
        self.assertIn("p.triage_class <> 'female_candidate'", sql)

    def test_gate1b_has_three_queues_and_policy_only_bulk_suppresses_female_candidates(
        self,
    ):
        sql = (
            Path("supabase/migrations/20260811220000_gate1b_triage.sql")
            .read_text()
            .lower()
        )
        for queue in ("likely_male", "uncertain", "female_audit", "suppressed"):
            self.assertIn(f"'{queue}'", sql)
        self.assertIn("and suppression_enabled then 'suppressed'", sql)
        self.assertIn("female_audit_percent integer not null default 10", sql)
        self.assertIn("gate1b_queue <> 'suppressed'", sql)

    def test_gate1b_round_robins_cameras_and_prioritizes_hd_without_auto_request(self):
        sql = (
            Path("supabase/migrations/20260811220000_gate1b_triage.sql")
            .read_text()
            .lower()
        )
        self.assertIn("row_number() over (partition by m.camera_id", sql)
        self.assertIn("hd_recommended boolean not null default false", sql)
        self.assertIn("order by h.priority desc, h.created_at, h.id", sql)
        recording = sql.split(
            "create or replace function public.deerid_record_gate1b_batch", 1
        )[1].split("create or replace function public.deerid_record_gate1b_label", 1)[0]
        self.assertNotIn("insert into deerid.hd_requests", recording)

    def test_gate1b_rpcs_are_service_role_only(self):
        sql = (
            Path("supabase/migrations/20260811220000_gate1b_triage.sql")
            .read_text()
            .lower()
        )
        signatures = (
            "public.deerid_gate1b_pending(text, text, integer)",
            "public.deerid_record_gate1b_batch(text, text, jsonb)",
            "public.deerid_record_gate1b_label(uuid, bigint, integer, text, text, text, text, text)",
            "public.deerid_gate1b_metrics()",
        )
        for signature in signatures:
            self.assertIn(
                f"revoke all on function {signature} from public, anon, authenticated",
                sql,
            )
            self.assertIn(f"grant execute on function {signature} to service_role", sql)

    def test_gate1b_library_fair_shares_the_bounded_payload_across_three_queues(self):
        sql = (
            Path("supabase/migrations/20260811223000_gate1b_queue_fair_share.sql")
            .read_text()
            .lower()
        )
        self.assertIn("partition by case", sql)
        self.assertIn("then gate1b_queue", sql)
        self.assertIn("order by review_priority, queue_row, queue_priority", sql)
        self.assertIn("gate1b_queue <> 'suppressed'", sql)
        self.assertIn("least(coalesce(p_limit, 60), 60)", sql)

    def test_gate1b_validation_batch_balances_camera_and_review_archive_strata(self):
        sql = (
            Path(
                "supabase/migrations/20260811224500_gate1b_balanced_validation_batch.sql"
            )
            .read_text()
            .lower()
        )
        self.assertIn("partition by m.camera_id, g.route", sql)
        self.assertIn("g.route in ('review', 'archive')", sql)
        self.assertIn("order by stratum_rank, camera_id", sql)

    def test_gate1b_suppression_is_pinned_validated_and_fail_closed(self):
        sql = (
            Path("supabase/migrations/20260811230000_gate1b_fail_closed_policy.sql")
            .read_text()
            .lower()
        )
        self.assertIn("model_version text not null", sql)
        self.assertIn("gate1b_policy_fail_closed", sql)
        self.assertIn("gate1b_label_rechecks_policy", sql)
        self.assertIn("minimum_axis_buck_events", sql)
        self.assertIn("axis_buck_retention_recall", sql)
        self.assertIn("coalesce((item->>'animal_count')::integer, 0) < 1", sql)
        self.assertIn("assessment.media_id <> (item->>'media_id')::uuid", sql)
        self.assertIn("assessment.event_key is distinct from item->>'event_key'", sql)
        self.assertIn(
            "revoke insert, update, delete, truncate on deerid.gate1b_predictions",
            sql,
        )

    def test_human_corrections_can_elevate_but_never_suppress(self):
        sql = (
            Path("supabase/migrations/20260811230500_gate1b_pinned_review_routing.sql")
            .read_text()
            .lower()
        )
        self.assertIn("gp.model_version = policy.model_version", sql)
        self.assertIn("human_antler = 'yes' or human_male = 'yes'", sql)
        self.assertNotIn("human_head = 'full' then 'female_candidate'", sql)

    def test_hd_priority_is_pinned_to_the_validated_model_version(self):
        sql = (
            Path("supabase/migrations/20260811231000_gate1b_pinned_hd_priority.sql")
            .read_text()
            .lower()
        )
        self.assertIn("gp.model_name = policy.model_name", sql)
        self.assertIn("gp.model_version = policy.model_version", sql)

    def test_validation_policy_has_immutable_safety_floors(self):
        sql = (
            Path(
                "supabase/migrations/20260811231500_gate1b_validation_safety_floor.sql"
            )
            .read_text()
            .lower()
        )
        for floor in (
            "minimum_labels >= 100",
            "minimum_buck_events >= 20",
            "minimum_whitetail_labels >= 10",
            "minimum_axis_labels >= 10",
            "minimum_whitetail_buck_events >= 10",
            "minimum_axis_buck_events >= 5",
            "required_buck_recall >= 0.99",
            "female_audit_percent >= 10",
        ):
            self.assertIn(floor, sql)

    def test_prediction_inserts_recheck_and_disable_suppression(self):
        sql = (
            Path("supabase/migrations/20260811232000_gate1b_prediction_fail_closed.sql")
            .read_text()
            .lower()
        )
        self.assertIn("after insert on deerid.gate1b_predictions", sql)
        self.assertIn("gate1b_recheck_suppression", sql)
        self.assertIn("set suppression_enabled = false", sql)

    def test_profile_assignment_is_human_confirmed_season_scoped_and_service_only(self):
        sql = (
            Path("supabase/migrations/20260811233000_profile_assignment.sql")
            .read_text()
            .lower()
        )
        self.assertIn("deerid_create_profile_from_review", sql)
        self.assertIn("deerid_attach_media_to_profile_from_review", sql)
        self.assertIn("confirmation_status", sql)
        self.assertIn("'confirmed'", sql)
        self.assertIn("extract(year from media_captured_at)", sql)
        self.assertIn("or review_state.resolved", sql)
        for signature in (
            "public.deerid_profiles()",
            "public.deerid_create_profile_from_review(uuid, bigint, integer, text, text, text, text)",
            "public.deerid_attach_media_to_profile_from_review(uuid, bigint, integer, uuid)",
        ):
            self.assertIn(
                f"revoke all on function {signature} from public, anon, authenticated",
                sql,
            )
            self.assertIn(f"grant execute on function {signature} to service_role", sql)

    def test_profile_assignment_corrective_migration_is_fresh_safe_auditable_and_idempotent(self):
        sql = (
            Path("supabase/migrations/20260811234000_profile_assignment_hardening.sql")
            .read_text()
            .lower()
        )
        self.assertIn("create table deerid.profile_assignment_events", sql)
        self.assertIn("profile_assignment_events_are_append_only", sql)
        self.assertIn("prior_snapshot", sql)
        self.assertIn("resulting_snapshot", sql)
        self.assertIn("insert into deerid.gate1_review_state", sql)
        self.assertIn("on conflict (gate1_assessment_id) do nothing", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("'created', false", sql)
        self.assertIn("unique (animal_profile_id, media_id, gate1_assessment_id, review_version, action)", sql)
        self.assertGreaterEqual(sql.count("on delete restrict"), 3)
        self.assertIn("alter table deerid.profile_assignment_events enable row level security", sql)
        self.assertIn("grant execute on function public.deerid_create_profile_from_review", sql)
        self.assertIn("grant execute on function public.deerid_attach_media_to_profile_from_review", sql)


if __name__ == "__main__":
    unittest.main()
