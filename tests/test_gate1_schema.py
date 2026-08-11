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
        hardening = sorted(Path("supabase/migrations").glob("*_gate1_hardening.sql"))[-1].read_text().lower()
        self.assertIn("gate1_assessment_model_once_idx", hardening)
        self.assertIn("gate1_review_state", hardening)
        self.assertIn("p_review_version", hardening)
        self.assertIn("stale or resolved review capability", hardening)
        self.assertIn("event_start", hardening)
        self.assertIn("candidate_events", hardening)
        self.assertIn("queue_priority", hardening)

    def test_pending_hardening_filters_variants_and_bounds_complete_events(self):
        sql = Path("supabase/migrations/20260811031500_gate1_pending_hardening.sql").read_text().lower()
        self.assertIn("m.variant = 'cloud_thumbnail'", sql)
        self.assertIn("extract(epoch from event_start)", sql)
        self.assertIn("/ 10", sql)
        self.assertIn("limit least(greatest(p_limit, 1), 50)", sql)

    def test_pipeline_funnel_rpc_counts_each_gate_for_one_model_version(self):
        sql = Path("supabase/migrations/20260811153000_gate1_funnel.sql").read_text().lower()
        self.assertIn("function public.deerid_gate1_funnel", sql)
        for field in (
            "total_thumbnails", "assessed_thumbnails", "pending_thumbnails",
            "review_representatives", "event_duplicates", "archived",
            "unresolved_review", "resolved_review",
        ):
            self.assertIn(field, sql)
        self.assertIn("m.variant = 'cloud_thumbnail'", sql)
        self.assertIn("g.model_name = p_model_name", sql)
        self.assertIn("g.model_version = p_model_version", sql)
        self.assertIn("grant execute", sql)
        self.assertIn("service_role", sql)

    def test_pipeline_funnel_breaks_archives_into_blank_and_non_target_reasons(self):
        sql = Path("supabase/migrations/20260811154500_gate1_funnel_reasons.sql").read_text().lower()
        self.assertIn("blank_or_below_threshold", sql)
        self.assertIn("confident_non_target", sql)
        self.assertIn("reason", sql)

    def test_gate1_claims_are_atomic_leased_and_released(self):
        sql = Path("supabase/migrations/20260811162000_gate1_claim_leases.sql").read_text().lower()
        self.assertIn("create table deerid.gate1_claims", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("leased_until", sql)
        self.assertIn("claim_token", sql)
        self.assertIn("deerid_release_gate1_claim", sql)
        self.assertIn("grant execute", sql)
        self.assertIn("service_role", sql)

    def test_gate1_recording_is_fenced_and_claims_complete_events(self):
        sql = Path("supabase/migrations/20260811163500_gate1_claim_fencing.sql").read_text().lower()
        self.assertIn("media_ids uuid[]", sql)
        self.assertIn("p_claim_token uuid", sql)
        self.assertIn("for update", sql)
        self.assertIn("delete from deerid.gate1_claims", sql)
        self.assertIn("cardinality", sql)
        self.assertIn("jsonb_array_length", sql)

    def test_gate1_event_membership_is_persisted_before_claiming(self):
        sql = Path("supabase/migrations/20260811165000_gate1_stable_events.sql").read_text().lower()
        self.assertIn("create table deerid.gate1_event_memberships", sql)
        self.assertIn("primary key (media_id)", sql)
        self.assertIn("deerid_assign_gate1_events", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("count(*) < 10", sql)
        self.assertIn("gate1_event_memberships em", sql)


if __name__ == "__main__":
    unittest.main()
