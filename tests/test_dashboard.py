import unittest
from datetime import datetime, timezone

from reveal_downloader.dashboard import handle_preview, handle_status, record_dashboard_run


VALID_PREVIEW_PATH = (
    "cam-1@" + "a" * 64 + "/2026/08/07/20260807T100000Z_photo-1@" + "b" * 64 + ".jpg"
)


class MemoryArchive:
    def __init__(self, runs=None):
        self.runs = runs or []
        self.saved = None
        self.deadline = None

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline
        self.clock = clock

    def read_dashboard_runs(self, limit=20):
        return self.runs[:limit]

    def write_dashboard_run(self, run):
        self.saved = run

    def read_private_image(self, object_path, max_bytes):
        self.read_image_args = (object_path, max_bytes)
        return b"\xff\xd8preview\xff\xd9"


class DashboardRunTests(unittest.TestCase):
    def test_record_run_writes_one_immutable_record_with_verified_units(self):
        archive = MemoryArchive()
        now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

        run = record_dashboard_run(
            archive,
            status="healthy",
            downloaded=1,
            skipped=2,
            failed=0,
            archive_units=[
                {"object_path": "private/camera/photo.jpg", "captured_at": "2026-08-07T10:00:00Z"}
            ],
            now=now,
        )

        self.assertIs(archive.saved, run)
        self.assertEqual(run["version"], 1)
        self.assertEqual(run["finished_at"], "2026-08-07T12:00:00Z")
        self.assertEqual(run["status"], "healthy")
        self.assertEqual(run["verified"], {"image": 3, "metadata": 3, "checksum": 3})
        self.assertEqual(run["recent_units"][0]["object_path"], "private/camera/photo.jpg")

    def test_record_run_bounds_and_deduplicates_units_without_read_modify_write(self):
        archive = MemoryArchive([{"status": "must-not-be-read"}])

        run = record_dashboard_run(
            archive,
            status="degraded",
            downloaded=0,
            skipped=0,
            failed=1,
            archive_units=[
                {"object_path": "same.jpg", "captured_at": "2026-08-07T10:00:00Z"},
                {"object_path": "same.jpg", "captured_at": "2026-08-07T11:00:00Z"},
                *[
                    {"object_path": f"new-{index}.jpg", "captured_at": "2026-08-07T10:00:00Z"}
                    for index in range(12)
                ],
            ],
        )

        self.assertEqual(len(run["recent_units"]), 10)
        self.assertEqual(
            [unit["object_path"] for unit in run["recent_units"]].count("same.jpg"),
            1,
        )
        self.assertEqual(run["recent_units"][0]["captured_at"], "2026-08-07T10:00:00Z")


class DashboardStatusTests(unittest.TestCase):
    @staticmethod
    def _environment():
        return {
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SECRET_KEY": "secret",
        }

    def test_status_returns_only_sanitized_fields(self):
        records = [{
            "version": 1,
            "id": "internal-run-id",
            "finished_at": "2026-08-07T12:00:00Z",
            "status": "healthy",
            "downloaded": 2,
            "skipped": 3,
            "failed": 0,
            "verified": {"image": 5, "metadata": 5, "checksum": 5},
            "secret": "must-not-leak",
            "recent_units": [{
                "object_path": "private/camera/photo.jpg",
                "captured_at": "2026-08-07T10:00:00Z",
                "gps": "must-not-leak",
            }],
        }]
        archive = MemoryArchive(records)

        status, payload = handle_status(
            self._environment(), archive_factory=lambda *_: archive
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["health"], "healthy")
        self.assertEqual(payload["updated_at"], "2026-08-07T12:00:00Z")
        self.assertEqual(payload["latest"]["verified"]["checksum"], 5)
        self.assertNotIn("previews_enabled", payload)
        self.assertNotIn("previews", payload)
        serialized = str(payload)
        self.assertNotIn("private/camera", serialized)
        self.assertNotIn("gps", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("internal-run-id", serialized)

    def test_status_propagates_one_bounded_monotonic_deadline(self):
        archive = MemoryArchive([])

        status, _ = handle_status(
            self._environment(), archive_factory=lambda *_: archive, now=100.0
        )

        self.assertEqual(status, 200)
        self.assertEqual(archive.deadline, 108.0)
        self.assertEqual(archive.clock(), 100.0)

    def test_enabled_status_returns_expiring_opaque_preview_urls_without_paths(self):
        records = [{
            "version": 1,
            "id": "c" * 32,
            "finished_at": "2026-08-07T12:00:00Z",
            "status": "healthy",
            "downloaded": 1,
            "skipped": 0,
            "failed": 0,
            "verified": {"image": 1, "metadata": 1, "checksum": 1},
            "recent_units": [{
                "object_path": VALID_PREVIEW_PATH,
                "captured_at": "2026-08-07T10:00:00Z",
            }],
        }]
        archive = MemoryArchive(records)
        environ = {
            **self._environment(),
            "CRON_SECRET": "preview-signing-secret-at-least-16",
            "PREVIEWS_ENABLED": "true",
        }

        status, payload = handle_status(
            environ, archive_factory=lambda *_: archive, epoch_now=1_786_100_000
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["previews_enabled"])
        self.assertEqual(len(payload["previews"]), 1)
        self.assertTrue(payload["previews"][0]["url"].startswith("/api/preview?token="))
        self.assertEqual(payload["previews"][0]["captured_at"], "2026-08-07T10:00:00Z")
        self.assertNotIn(VALID_PREVIEW_PATH, str(payload))

    def test_preview_proxy_validates_token_path_and_image(self):
        records = [{
            "version": 1,
            "id": "c" * 32,
            "finished_at": "2026-08-07T12:00:00Z",
            "status": "healthy",
            "downloaded": 1,
            "skipped": 0,
            "failed": 0,
            "verified": {"image": 1, "metadata": 1, "checksum": 1},
            "recent_units": [{"object_path": VALID_PREVIEW_PATH}],
        }]
        archive = MemoryArchive(records)
        environ = {
            **self._environment(),
            "CRON_SECRET": "preview-signing-secret-at-least-16",
            "PREVIEWS_ENABLED": "true",
        }
        _, status_payload = handle_status(
            environ, archive_factory=lambda *_: archive, epoch_now=1_786_100_000
        )
        token = status_payload["previews"][0]["url"].split("token=", 1)[1]

        status, content_type, body = handle_preview(
            environ,
            token,
            archive_factory=lambda *_: archive,
            epoch_now=1_786_100_001,
        )

        self.assertEqual((status, content_type), (200, "image/jpeg"))
        self.assertEqual(body, b"\xff\xd8preview\xff\xd9")
        self.assertEqual(archive.read_image_args[0], VALID_PREVIEW_PATH)
        self.assertEqual(handle_preview(environ, token + "x", archive_factory=lambda *_: archive, epoch_now=1_786_100_001)[0], 404)
        self.assertEqual(handle_preview(environ, token, archive_factory=lambda *_: archive, epoch_now=1_786_100_301)[0], 404)

    def test_preview_proxy_rejects_poisoned_private_object_path(self):
        records = [{
            "version": 1,
            "id": "c" * 32,
            "finished_at": "2026-08-07T12:00:00Z",
            "status": "healthy",
            "downloaded": 1,
            "skipped": 0,
            "failed": 0,
            "verified": {"image": 1, "metadata": 1, "checksum": 1},
            "recent_units": [{"object_path": "../../private/secret.jpg"}],
        }]
        archive = MemoryArchive(records)
        environ = {
            **self._environment(),
            "CRON_SECRET": "preview-signing-secret-at-least-16",
            "PREVIEWS_ENABLED": "true",
        }

        status, payload = handle_status(
            environ, archive_factory=lambda *_: archive, epoch_now=1_786_100_000
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["previews"], [])

    def test_status_rejects_poisoned_or_non_utc_finished_timestamp(self):
        for poisoned in (
            "2026-08-07T12:00:00Z /private/path secret GPS=30,-100",
            "2026-08-07T12:00:00+01:00",
            "x" * 1000,
            "not-a-date",
        ):
            with self.subTest(poisoned=poisoned):
                archive = MemoryArchive([{
                    "version": 1,
                    "finished_at": poisoned,
                    "status": "healthy",
                    "downloaded": 1,
                    "skipped": 0,
                    "failed": 0,
                    "verified": {"image": 1, "metadata": 1, "checksum": 1},
                }])

                status, payload = handle_status(
                    self._environment(), archive_factory=lambda *_: archive
                )

                self.assertEqual(status, 503)
                self.assertEqual(payload, {"ok": False, "error": "status unavailable"})
                self.assertNotIn(poisoned, str(payload))


if __name__ == "__main__":
    unittest.main()
