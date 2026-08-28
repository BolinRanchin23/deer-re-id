import json
from pathlib import Path
import unittest

from reveal_downloader.client import RevealError
from reveal_downloader.supabase import SupabaseArchive
from tests.test_supabase import FakeRevealClient, FakeStorageTransport


MIGRATIONS = Path("supabase/migrations")


class CatalogSchemaTests(unittest.TestCase):
    def test_all_photos_forces_parameter_aware_query_plans(self):
        sql = (MIGRATIONS / "20260828181500_optimize_all_photos_query.sql").read_text()
        self.assertIn("deerid_all_photos", sql)
        self.assertIn("plan_cache_mode", sql)
        self.assertIn("force_custom_plan", sql)

    def test_all_photos_executes_with_a_fresh_parameter_aware_plan(self):
        sql = (MIGRATIONS / "20260828183000_replan_all_photos_per_request.sql").read_text().lower()
        self.assertIn("language plpgsql", sql)
        self.assertIn("execute $query$", sql)
        self.assertIn("using p_limit", sql)
        self.assertIn("security definer", sql)
        self.assertIn("set search_path=pg_catalog,public,deerid,pg_temp", sql)

    def test_profile_gallery_page_is_profile_scoped_and_hard_bounded(self):
        sql = Path("supabase/migrations/20260817190000_bounded_profile_gallery_page.sql").read_text().lower()
        self.assertIn("deerid_profile_gallery_page", sql)
        self.assertIn("p_profile_id uuid", sql)
        self.assertIn("am.animal_profile_id=p_profile_id", sql)
        self.assertIn("least(coalesce(p_limit,24),60)", sql)
    def test_all_photos_followup_migration_implements_filters_and_uses_last_returned_cursor(self):
        sql = Path("supabase/migrations/20260812221000_fix_all_photos_paging_filters.sql").read_text()
        for field in ("p_time_of_day", "p_species", "p_male_antler", "p_profile_status", "p_identity_status", "p_sort"):
            self.assertIn(field, sql)
        self.assertIn("from ordered_page", sql)
        self.assertNotIn("offset greatest(1,least(p_limit,60))", sql)

    def test_profiling_progress_is_authoritative_by_camera(self):
        sql = Path("supabase/migrations/20260812221500_profiling_location_progress.sql").read_text()
        self.assertIn("'camera_id',m.camera_id", sql)
        self.assertIn("'by_camera',by_camera.value", sql)
        js = Path("public/app.js").read_text()
        self.assertIn("n(byCamera[locationId])", js)
        self.assertNotIn("locationQueue.length || remaining", js)

    def test_profile_previews_and_model_evidence_are_authoritative(self):
        sql = Path("supabase/migrations/20260812222000_authoritative_metrics_profile_previews.sql").read_text()
        self.assertIn("profile_previews", sql)
        self.assertIn("not exists(select 1 from deerid.hd_instance_profile_assignment_events n where n.supersedes_event_id=e.id)", sql)
        self.assertIn("p.model_name='OpenAI-GPT-4o-mini-Vision'", sql)
        self.assertIn("p.model_version='gpt-4o-mini-2024-07-18@prompt-2026-08-12.1'", sql)

    def test_profiling_queue_is_bounded_per_camera_not_globally(self):
        sql = Path("supabase/migrations/20260812222500_profiling_queue_per_camera.sql").read_text()
        self.assertIn("row_number() over(partition by m.camera_id", sql)
        self.assertIn("where camera_rank<=", sql)

    def test_authorized_hd_incident_replay_is_one_shot_and_bounded(self):
        sql = Path("supabase/migrations/20260813113500_authorize_hd_incident_replay.sql").read_text()
        self.assertIn("hd_review_retry_authorizations", sql)
        self.assertIn("delete from deerid.hd_review_retry_authorizations where media_asset_id=chosen.id", sql)
        self.assertIn("not exists(select 1 from deerid.hd_review_results", sql)
        self.assertIn("p_media_asset_id is null or a.id=p_media_asset_id", sql)

    def test_authorized_hd_incident_replay_skips_assets_absent_from_fresh_databases(self):
        sql = Path("supabase/migrations/20260813113500_authorize_hd_incident_replay.sql").read_text().lower()
        self.assertIn("join deerid.media_assets", sql)

    def test_operational_gate1b_policy_tracks_current_pinned_model_and_backfills_hd(self):
        sql = Path("supabase/migrations/20260812223000_align_operational_gate1b_policy.sql").read_text()
        self.assertIn("OpenAI-GPT-4o-mini-Vision", sql)
        self.assertIn("gpt-4o-mini-2024-07-18@prompt-2026-08-12.1", sql)
        self.assertIn("apply_gate1b_automation_prediction", sql)

    def test_automatic_hd_completion_does_not_require_human_review_state(self):
        sql = Path("supabase/migrations/20260812223500_complete_automatic_hd_requests.sql").read_text().lower()
        self.assertIn("gate1b_automatic_likely_male", sql)
        self.assertIn("status = 'submitted'", sql)
        self.assertIn("review_version is null", sql)

    def test_profiling_queue_page_is_location_scoped_and_separates_deferred_items(self):
        migration = Path("supabase/migrations/20260828023500_profiling_review_loop.sql")
        self.assertTrue(migration.exists())
        sql = migration.read_text().lower()
        self.assertIn("deerid_hd_review_queue_page", sql)
        self.assertIn("p_camera_id uuid", sql)
        self.assertIn("p_queue text", sql)
        self.assertIn("'active'", sql)
        self.assertIn("'deferred'", sql)

    def test_profiling_progress_is_instance_scoped_and_reports_each_buffer(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text().lower()
        progress = sql.split("create or replace function public.deerid_hd_review_progress", 1)[1]
        for field in ("profiling_ready", "deferred", "pending_confirmation", "detector_errors", "by_camera"):
            self.assertIn(f"'{field}'", progress)
        self.assertIn("e.hd_animal_instance_id=i.id", progress)
        self.assertNotIn("assigned_i.media_asset_id", progress)

    def test_profiling_workflow_actions_are_append_only_and_instance_scoped(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text().lower()
        self.assertIn("create table deerid.hd_instance_review_events", sql)
        self.assertIn("deerid_record_hd_review_workflow_action", sql)
        self.assertIn("p_hd_animal_instance_id uuid", sql)
        self.assertIn("p_hd_review_result_id bigint", sql)
        self.assertIn("'detector_error'", sql)
        self.assertIn("for update", sql)

    def test_detector_issue_queue_carries_the_durable_reason_and_note(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text(encoding="utf-8").lower()
        self.assertIn("workflow_reason", sql)
        self.assertIn("workflow_note", sql)

    def test_release_runbook_requires_migration_and_rpc_verification_before_vercel(self):
        runbook = Path("docs/profiling-review-loop-release.md")
        self.assertTrue(runbook.exists())
        text = runbook.read_text(encoding="utf-8").lower()
        self.assertLess(text.index("supabase db push"), text.index("vercel deploy"))
        self.assertIn("deerid_pipeline_health", text)
        self.assertIn("deerid_hd_review_queue_page", text)

    def test_profile_gallery_excludes_legacy_rows_without_identity_crops(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text(encoding="utf-8").lower()
        gallery = sql.split("create or replace function public.deerid_profile_gallery_page", 1)[1]
        self.assertIn("and i.id is not null", gallery)

    def test_topology_corrections_are_append_only_idempotent_and_queue_aware(self):
        migration = Path("supabase/migrations/20260828150500_hd_instance_topology_corrections.sql")
        self.assertTrue(migration.exists())
        sql = migration.read_text(encoding="utf-8").lower()
        self.assertIn("create table deerid.hd_instance_topology_events", sql)
        self.assertIn("request_id uuid not null unique", sql)
        self.assertIn("supersedes_event_id", sql)
        self.assertIn("resulting_instance_ids", sql)
        self.assertIn("deerid_correct_hd_instance_topology", sql)
        for action in ("'add'", "'split'", "'remove'", "'inseparable'"):
            self.assertIn(action, sql)
        self.assertIn("for update", sql)
        self.assertIn("row_number() over", sql)
        self.assertIn("count(*) over", sql)
        self.assertIn("topology_action not in ('split','remove','inseparable')", sql)
        self.assertIn("deerid_hd_review_queue_page", sql)
        self.assertIn("deerid_hd_review_progress", sql)
        self.assertIn("human_topology_correction", sql)
        self.assertIn("origin_kind", sql)
        self.assertIn("analysis_status", sql)
        self.assertIn("split_from_hd_animal_instance_id", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("existing.supersedes_event_id is distinct from p_expected_topology_event_id", sql)
        self.assertIn("duplicate topology box", sql)
        self.assertIn("lock_hd_review_result_media_asset", sql)
        self.assertIn("hd_review_results_media_asset_lock", sql)
        self.assertIn("revoke all on function public.deerid_hd_review_queue(integer) from service_role", sql)
        self.assertIn("reject_terminal_topology_mutation", sql)
        for table in ("hd_review_decisions", "hd_profile_assignment_proposals", "hd_instance_geometry_events", "hd_instance_review_events"):
            self.assertIn(f"on deerid.{table}", sql)

    def test_profile_assignments_enter_an_append_only_confirmation_buffer(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text().lower()
        self.assertIn("create table deerid.hd_profile_assignment_proposals", sql)
        self.assertIn("create table deerid.hd_profile_assignment_proposal_events", sql)
        self.assertIn("deerid_propose_hd_profile_assignment", sql)
        self.assertIn("deerid_confirm_hd_profile_assignment", sql)
        self.assertIn("deerid_undo_hd_profile_assignment", sql)
        self.assertIn("'pending'", sql)
        self.assertIn("'confirmed'", sql)
        self.assertIn("'undone'", sql)

    def test_box_corrections_preserve_original_geometry_and_append_revisions(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text().lower()
        self.assertIn("create table deerid.hd_instance_geometry_events", sql)
        self.assertIn("deerid_correct_hd_instance_bbox", sql)
        self.assertIn("p_expected_geometry_event_id bigint", sql)
        self.assertIn("bbox_x + bbox_width <= 1", sql)
        self.assertIn("bbox_y + bbox_height <= 1", sql)
        self.assertIn("geometry events are append-only", sql)

    def test_pipeline_health_reports_every_authoritative_stage_and_telemetry_gap(self):
        sql = Path("supabase/migrations/20260828023500_profiling_review_loop.sql").read_text().lower()
        self.assertIn("deerid_pipeline_health", sql)
        for stage in ("ingestion", "gate1", "gate1b", "hd_requests", "hd_returns", "hd_analysis", "profiling"):
            self.assertIn(f"'{stage}'", sql)
        for field in ("last_success_at", "pending_count", "oldest_pending_at", "stale_claim_count", "failure_count_24h", "telemetry_complete"):
            self.assertIn(f"'{field}'", sql)

    def test_refresh_migrations_define_metrics_paged_photos_and_representatives(self):
        sql = "\n".join(path.read_text().lower() for path in MIGRATIONS.glob("20260812*site_refresh*.sql"))
        for required in ("deerid_process_overview", "deerid_all_photos", "profile_representative_events", "deerid_set_profile_representative", "p_camera_id", "p_cursor"):
            self.assertIn(required, sql)
        self.assertIn("camera_id", sql)
        self.assertIn("first_seen", sql)
        self.assertIn("last_seen", sql)

    def test_profile_gallery_and_reassignment_are_instance_scoped_and_auditable(self):
        sql = (MIGRATIONS / "20260812150000_hd_review_profile_gallery.sql").read_text(encoding="utf-8").lower()
        for required in (
            "create table deerid.hd_instance_profile_assignment_events",
            "supersedes_event_id bigint",
            "create or replace function public.deerid_profile_gallery",
            "create or replace function public.deerid_reassign_hd_instance",
            "crop_recipe",
            "hd_animal_instance_id",
        ):
            self.assertIn(required, sql)
        self.assertIn("action in ('assign','reassign')", sql)
        self.assertIn("order by e.created_at desc,e.id desc", sql)

    def test_hd_queue_uses_latest_analysis_and_hides_only_the_resolved_or_assigned_instance(self):
        sql = (MIGRATIONS / "20260828023500_profiling_review_loop.sql").read_text(encoding="utf-8").lower()
        queue = sql.split("create or replace function public.deerid_hd_review_queue_page", 1)[1]
        self.assertIn("distinct on (r.media_asset_id)", queue)
        self.assertIn("hd_instance_profile_assignment_events", queue)
        self.assertIn("e.hd_animal_instance_id=i.id", queue)
        self.assertNotIn("assigned_i.media_asset_id=r.media_asset_id", queue)
        self.assertIn("d.action<>'defer'", queue)

    def test_gpt_rerun_claim_excludes_only_assets_classified_to_profiles(self):
        sql = (MIGRATIONS / "20260812150000_hd_review_profile_gallery.sql").read_text(encoding="utf-8").lower()
        claim = sql.split("create or replace function public.deerid_claim_hd_review", 1)[1].split("create or replace function public.deerid_hd_review_queue", 1)[0]
        self.assertIn("hd_instance_profile_assignment_events", claim)
        self.assertIn("current_event.animal_profile_id is not null", claim)
        self.assertNotIn("hd_review_decisions d", claim)

    def test_multi_animal_hd_migration_creates_instance_scoped_review_and_assignments(self):
        sql = (MIGRATIONS / "20260812010000_multi_animal_hd_review.sql").read_text(encoding="utf-8").lower()
        for required in (
            "create table deerid.hd_animal_instances",
            "bbox_x double precision",
            "detection_complete boolean",
            "hd_animal_instance_id uuid",
            "unique (hd_animal_instance_id)",
            "create or replace function public.deerid_complete_hd_review",
            "create or replace function public.deerid_hd_review_queue",
            "create or replace function public.deerid_record_hd_review_decision",
        ):
            self.assertIn(required, sql)
        self.assertIn("check (bbox_x + bbox_width <= 1)", sql)
        self.assertIn("p_hd_animal_instance_id uuid", sql)

    def test_unsafe_legacy_multi_animal_backfill_is_removed_before_review(self):
        sql = Path("supabase/migrations/20260812011500_remove_unsafe_legacy_multi_animal_backfill.sql").read_text()
        self.assertIn("coalesce((r.result->>'animal_count')::integer, 1) > 1", sql)
        self.assertIn("crop_recipe->>'source' = 'legacy_whole_frame'", sql)
        self.assertIn("not exists", sql.lower())
        self.assertIn("hd_review_decisions", sql)
        self.assertIn("create trigger hd_animal_instances_append_only", sql)

    def test_timestamp_parser_is_correctly_marked_stable(self):
        migration = MIGRATIONS / "20260810232827_timestamp_parser_stability.sql"
        sql = migration.read_text(encoding="utf-8").lower()
        self.assertIn("alter function deerid.try_timestamptz(text) stable", sql)

    def test_review_fix_migration_handles_observed_reveal_shapes_and_local_collections(self):
        migration = MIGRATIONS / "20260810234439_catalog_review_fixes.sql"
        sql = migration.read_text(encoding="utf-8")
        lower = sql.lower()
        self.assertIn("drop constraint if exists collections_collection_type_provider_collection_id_key", lower)
        self.assertIn("where provider_collection_id is not null", lower)
        for field in (
            "cameraLocation",
            "cameraWarrantyEndDate",
            "{metadata,batteryLevel}",
            "{metadata,signal}",
            "weatherRecord",
            "weatherLabel",
            "barometricPressure",
            "windDirection",
            "temperatureRange12Hours",
        ):
            self.assertIn(field, sql)

    def test_top_level_camera_status_payload_is_never_inserted_as_null(self):
        migration = MIGRATIONS / "20260810234858_camera_status_raw_payload.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("coalesce(c->'status', c)", sql)

    def test_reingestion_refreshes_all_structured_status_and_weather_fields(self):
        migration = MIGRATIONS / "20260810235551_refresh_observation_fields.sql"
        sql = migration.read_text(encoding="utf-8")
        for assignment in (
            "battery_level = excluded.battery_level",
            "signal_level = excluded.signal_level",
            "internal_voltage = excluded.internal_voltage",
            "solar_percent = excluded.solar_percent",
            "pressure_tendency = excluded.pressure_tendency",
            "minimum_temperature_12h = excluded.minimum_temperature_12h",
            "maximum_temperature_12h = excluded.maximum_temperature_12h",
            "wind_direction_degrees = excluded.wind_direction_degrees",
            "wind_speed = excluded.wind_speed",
            "moon_phase = excluded.moon_phase",
        ):
            self.assertIn(assignment, sql)

    def test_latest_media_upsert_refreshes_mutable_identity_fields(self):
        sql = (
            MIGRATIONS / "20260811002525_refresh_media_identity_fields.sql"
        ).read_text(encoding="utf-8").lower()
        for field in (
            "provider_camera_id",
            "camera_id",
            "captured_at",
            "media_type",
            "ownership_type",
        ):
            self.assertIn(f"{field} = excluded.{field}", sql)

    def test_foundation_migration_defines_private_reveal_catalog(self):
        migrations = sorted(MIGRATIONS.glob("*_deerid_foundation.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8").lower()

        for table in (
            "deerid.cameras",
            "deerid.camera_locations",
            "deerid.camera_status_observations",
            "deerid.camera_settings_snapshots",
            "deerid.media",
            "deerid.media_weather",
            "deerid.media_labels",
            "deerid.animals",
            "deerid.animal_profiles",
            "deerid.animal_media",
            "deerid.collections",
            "deerid.collection_items",
            "deerid.classification_jobs",
            "deerid.ingestion_runs",
        ):
            self.assertIn("create table if not exists " + table, sql)
            self.assertIn("alter table " + table + " enable row level security", sql)

        self.assertIn("create or replace function public.deerid_ingest_reveal_batch", sql)
        self.assertIn("photo#>>'{gps,latitude}'", sql)
        self.assertIn("photo#>>'{gps,longitude}'", sql)
        self.assertIn("unique nulls not distinct (media_id, stage, model_name, model_version)", sql)
        self.assertIn("create or replace function public.deerid_private_library", sql)
        self.assertIn("create or replace function public.deerid_private_camera_map", sql)
        self.assertIn("create or replace function public.deerid_private_media_object", sql)
        self.assertIn("grant execute on function public.deerid_ingest_reveal_batch", sql)
        self.assertIn("to service_role", sql)
        self.assertNotIn("grant select on", sql)
        self.assertNotIn("to anon", sql)

        collection_items = sql.split(
            "create table if not exists deerid.collection_items", 1
        )[1].split(");", 1)[0]
        self.assertIn("id uuid primary key", collection_items)
        self.assertNotIn("primary key (collection_id, media_id, animal_id)", collection_items)
        self.assertIn("unique nulls not distinct (collection_id, media_id, animal_id)", collection_items)


class CatalogTransport(FakeStorageTransport):
    def __init__(self, *, rpc_status=200):
        super().__init__()
        self.rpc_status = rpc_status
        self.catalog_payloads = []
        self.catalog_headers = []

    def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
        if url.endswith("/rest/v1/rpc/deerid_ingest_reveal_batch"):
            self.calls.append((method, url, headers or {}, body))
            self.catalog_payloads.append(json.loads(body))
            self.catalog_headers.append(dict(headers or {}))
            from reveal_downloader.supabase import StorageResponse

            return StorageResponse(self.rpc_status, b'{"ok":true}')
        return super().request(
            method,
            url,
            headers=headers,
            body=body,
            max_response_bytes=max_response_bytes,
        )


class CameraRevealClient(FakeRevealClient):
    def __init__(self):
        super().__init__()
        self.camera_calls = 0

    def get_cameras(self):
        self.camera_calls += 1
        return [
            {
                "cameraId": "cam-1",
                "name": "North Ridge",
                "iccid": "carrier-secret",
                "fcmTokens": ["push-secret"],
                "gps": {"latitude": 30.1, "longitude": -100.2},
                "status": {"batteryLevel": 82, "signalLevel": 4},
                "settings": [{"code": "GPS", "value": "ON"}],
            }
        ]


class SupabaseCatalogTests(unittest.TestCase):
    def test_catalog_is_opt_in_and_does_not_add_upstream_calls_by_default(self):
        transport = CatalogTransport()
        client = CameraRevealClient()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )

        archive.sync(client, max_pages=1)

        self.assertEqual(client.camera_calls, 0)
        self.assertEqual(transport.catalog_payloads, [])

    def test_enabled_catalog_indexes_only_verified_archive_units(self):
        transport = CatalogTransport()
        client = CameraRevealClient()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(client, max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(client.camera_calls, 1)
        self.assertEqual(len(transport.catalog_payloads), 1)
        payload = transport.catalog_payloads[0]
        self.assertEqual(payload["p_cameras"][0]["cameraId"], "cam-1")
        self.assertEqual(payload["p_media"][0]["provider"]["photoId"], "p1")
        self.assertTrue(payload["p_media"][0]["object_path"].endswith(".jpg"))
        self.assertEqual(len(payload["p_media"][0]["image_sha256"]), 64)
        self.assertGreater(payload["p_media"][0]["image_bytes"], 0)
        serialized = json.dumps(payload)
        self.assertNotIn("SUPABASE_SECRET_KEY", serialized)
        self.assertNotIn("photoUrl", serialized)
        self.assertNotIn("carrier-secret", serialized)
        self.assertNotIn("push-secret", serialized)
        self.assertEqual(transport.catalog_headers[0].get("apikey"), "sb_secret_test")
        self.assertNotIn("Authorization", transport.catalog_headers[0])

    def test_catalog_failure_degrades_but_does_not_undo_verified_archive(self):
        transport = CatalogTransport(rpc_status=404)
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(CameraRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.failure_stages, {"catalog_index": 1})
        self.assertEqual(len(archive.last_archive_units), 1)
        self.assertEqual(len(transport.catalog_payloads), 1)

    def test_camera_catalog_failure_does_not_block_photo_archive_or_media_index(self):
        class CameraFailureClient(CameraRevealClient):
            def get_cameras(self):
                raise RevealError("camera inventory unavailable")

        transport = CatalogTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(CameraFailureClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.failure_stages, {"catalog_cameras": 1})
        self.assertEqual(transport.catalog_payloads[0]["p_cameras"], [])
        self.assertEqual(len(transport.catalog_payloads[0]["p_media"]), 1)

    def test_unexpected_auxiliary_catalog_errors_never_mask_archive_success(self):
        class MalformedCameraClient(CameraRevealClient):
            def get_cameras(self):
                raise TypeError("malformed camera response")

        class BrokenCatalogTransport(CatalogTransport):
            def request(self, method, url, **kwargs):
                if url.endswith("/rest/v1/rpc/deerid_ingest_reveal_batch"):
                    raise ValueError("catalog serialization failed")
                return super().request(method, url, **kwargs)

        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=BrokenCatalogTransport(),
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(MalformedCameraClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 2)
        self.assertEqual(archive.failure_stages, {
            "catalog_cameras": 1,
            "catalog_index": 1,
        })
        self.assertEqual(len(archive.last_archive_units), 1)


if __name__ == "__main__":
    unittest.main()
