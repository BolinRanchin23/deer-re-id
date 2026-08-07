import time
import unittest

from reveal_downloader.client import RevealError
from reveal_downloader.supabase import StorageError
from reveal_downloader.vercel import handle_sync


class FakeClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password


class FakeArchive:
    deadline = None
    saved_run = None

    def __init__(self, url, key, bucket):
        self.settings = (url, key, bucket)
        self.last_archive_units = [
            {"object_path": "camera/recent.jpg", "captured_at": "2026-08-07T10:00:00Z"}
        ]
        self.progress_downloaded = 0
        self.progress_skipped = 0
        self.progress_failed = 0

    def sync(self, client, *, page_size, max_pages, deadline):
        from reveal_downloader.archive import SyncResult
        type(self).deadline = deadline
        return SyncResult(downloaded=2, skipped=3, failed=0)

    def write_dashboard_run(self, run):
        type(self).saved_run = run


class VercelSyncTests(unittest.TestCase):
    def test_degraded_sync_reports_safe_photo_failure_categories(self):
        class DegradedArchive(FakeArchive):
            failure_stages = {"image_host": 2}
            failure_hosts = {"cdn.example.test": 2}

            def sync(self, client, *, page_size, max_pages, deadline):
                from reveal_downloader.archive import SyncResult

                return SyncResult(downloaded=0, skipped=0, failed=2)

        status, payload = handle_sync(
            {
                "CRON_SECRET": "cron-secret-at-least-16",
                "TACTACAM_USERNAME": "person@example.com",
                "TACTACAM_PASSWORD": "tactacam-secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "supabase-secret",
            },
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=DegradedArchive,
        )

        self.assertEqual(status, 207)
        self.assertEqual(payload["failure_stages"], {"image_host": 2})
        self.assertEqual(payload["failure_hosts"], {"cdn.example.test": 2})

    def test_authorized_request_runs_cloud_sync(self):
        environ = {
            "CRON_SECRET": "cron-secret-at-least-16",
            "TACTACAM_USERNAME": "person@example.com",
            "TACTACAM_PASSWORD": "tactacam-secret",
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SECRET_KEY": "supabase-secret",
            "SUPABASE_BUCKET": "photos",
            "REVEAL_PAGE_SIZE": "50",
            "REVEAL_MAX_PAGES": "4",
        }

        status, payload = handle_sync(
            environ,
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=FakeArchive,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["downloaded"], 2)
        self.assertEqual(payload["skipped"], 3)
        deadline = FakeArchive.deadline
        self.assertIsInstance(deadline, float)
        assert isinstance(deadline, float)
        self.assertGreater(deadline, time.monotonic())
        self.assertLessEqual(deadline - time.monotonic(), 45)
        self.assertTrue(payload["status_recorded"])
        self.assertIsNotNone(FakeArchive.saved_run)
        self.assertEqual(FakeArchive.saved_run["status"], "healthy")
        self.assertEqual(
            FakeArchive.saved_run["recent_units"][0]["object_path"],
            "camera/recent.jpg",
        )

    def test_status_recording_failure_does_not_change_successful_sync_result(self):
        class TelemetryFailingArchive(FakeArchive):
            def write_dashboard_run(self, run):
                raise RuntimeError("telemetry unavailable")

        status, payload = handle_sync(
            {
                "CRON_SECRET": "cron-secret-at-least-16",
                "TACTACAM_USERNAME": "person@example.com",
                "TACTACAM_PASSWORD": "tactacam-secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "supabase-secret",
            },
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=TelemetryFailingArchive,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["status_recorded"])

    def test_upstream_failure_is_recorded_without_exposing_details(self):
        class FailingArchive(FakeArchive):
            def sync(self, client, *, page_size, max_pages, deadline):
                self.progress_downloaded = 2
                self.progress_skipped = 1
                self.progress_failed = 1
                raise RevealError("secret upstream detail")

        environ = {
            "CRON_SECRET": "cron-secret-at-least-16",
            "TACTACAM_USERNAME": "person@example.com",
            "TACTACAM_PASSWORD": "tactacam-secret",
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SECRET_KEY": "supabase-secret",
        }
        FailingArchive.saved_run = None

        status, payload = handle_sync(
            environ,
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=FailingArchive,
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload, {"ok": False, "error": "Reveal service failed"})
        self.assertEqual(FailingArchive.saved_run["status"], "error")
        self.assertEqual(FailingArchive.saved_run["downloaded"], 2)
        self.assertEqual(FailingArchive.saved_run["skipped"], 1)
        self.assertEqual(FailingArchive.saved_run["failed"], 1)
        self.assertEqual(len(FailingArchive.saved_run["recent_units"]), 1)
        self.assertNotIn("secret upstream detail", str(FailingArchive.saved_run))

    def test_storage_failure_is_reported_separately_without_details(self):
        class FailingStorageArchive(FakeArchive):
            def sync(self, client, *, page_size, max_pages, deadline):
                raise StorageError("Supabase bucket setup failed with HTTP 400")

        status, payload = handle_sync(
            {
                "CRON_SECRET": "cron-secret-at-least-16",
                "TACTACAM_USERNAME": "person@example.com",
                "TACTACAM_PASSWORD": "tactacam-secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "supabase-secret",
            },
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=FailingStorageArchive,
        )

        self.assertEqual(status, 502)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "error": "storage service failed",
                "storage_stage": "bucket_access",
                "storage_http_status": 400,
            },
        )
        self.assertNotIn("private storage detail", str(payload))

    def test_object_read_failure_reports_only_safe_stage_and_http_status(self):
        class ObjectReadFailingArchive(FakeArchive):
            def sync(self, client, *, page_size, max_pages, deadline):
                raise StorageError(
                    "Supabase object read failed with HTTP 400",
                    http_status=400,
                    provider_code="InvalidRequest",
                )

        status, payload = handle_sync(
            {
                "CRON_SECRET": "cron-secret-at-least-16",
                "TACTACAM_USERNAME": "person@example.com",
                "TACTACAM_PASSWORD": "tactacam-secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "supabase-secret",
            },
            "Bearer cron-secret-at-least-16",
            client_factory=FakeClient,
            archive_factory=ObjectReadFailingArchive,
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["storage_stage"], "object_read")
        self.assertEqual(payload["storage_http_status"], 400)
        self.assertEqual(payload["storage_provider_code"], "InvalidRequest")

    def test_request_with_wrong_cron_secret_is_rejected(self):
        status, payload = handle_sync(
            {"CRON_SECRET": "correct-secret-at-least-16"},
            "Bearer wrong-secret",
            client_factory=lambda *_: self.fail("client must not be created"),
            archive_factory=lambda *_: self.fail("archive must not be created"),
        )

        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_missing_cron_secret_fails_closed(self):
        status, payload = handle_sync({}, None)

        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

    def test_short_cron_secret_fails_closed(self):
        status, payload = handle_sync(
            {"CRON_SECRET": "too-short"},
            "Bearer too-short",
            client_factory=lambda *_: self.fail("client must not be created"),
            archive_factory=lambda *_: self.fail("archive must not be created"),
        )

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "CRON_SECRET must be at least 16 characters")


if __name__ == "__main__":
    unittest.main()
