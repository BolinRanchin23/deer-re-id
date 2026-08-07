import unittest

from reveal_downloader.vercel import handle_sync


class FakeClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password


class FakeArchive:
    def __init__(self, url, key, bucket):
        self.settings = (url, key, bucket)

    def sync(self, client, *, page_size, max_pages):
        from reveal_downloader.archive import SyncResult
        return SyncResult(downloaded=2, skipped=3, failed=0)


class VercelSyncTests(unittest.TestCase):
    def test_authorized_request_runs_cloud_sync(self):
        environ = {
            "CRON_SECRET": "cron-secret",
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
            "Bearer cron-secret",
            client_factory=FakeClient,
            archive_factory=FakeArchive,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["downloaded"], 2)
        self.assertEqual(payload["skipped"], 3)

    def test_request_with_wrong_cron_secret_is_rejected(self):
        status, payload = handle_sync(
            {"CRON_SECRET": "correct-secret"},
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


if __name__ == "__main__":
    unittest.main()
