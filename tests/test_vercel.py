import time
import unittest

from reveal_downloader.vercel import handle_sync


class FakeClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password


class FakeArchive:
    deadline = None

    def __init__(self, url, key, bucket):
        self.settings = (url, key, bucket)

    def sync(self, client, *, page_size, max_pages, deadline):
        from reveal_downloader.archive import SyncResult
        type(self).deadline = deadline
        return SyncResult(downloaded=2, skipped=3, failed=0)


class VercelSyncTests(unittest.TestCase):
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
