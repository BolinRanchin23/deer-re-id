import unittest

from reveal_downloader.client import RevealError
from reveal_downloader.hd_request_worker import run_worker


class FakeCatalog:
    def __init__(self, *args):
        self.claims = [
            {"ok": True, "empty": False, "request_token": "token-1", "provider_photo_id": "photo-1"},
            {"ok": True, "empty": False, "request_token": "token-2", "provider_photo_id": "photo-2"},
            {"ok": True, "empty": True},
        ]
        self.completed = []
        self.failed = []
        self.unknown = []

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline

    def claim_queued_hd_request(self):
        return self.claims.pop(0)

    def complete_hd_request(self, token):
        self.completed.append(token)
        return {"ok": True, "status": "submitted"}

    def fail_hd_request(self, token, code):
        self.failed.append((token, code))
        return {"ok": True, "status": "failed"}

    def mark_hd_request_unknown(self, token, code):
        self.unknown.append((token, code))
        return {"ok": True, "status": "unknown"}


class FakeReveal:
    requested = []

    def __init__(self, username, password):
        pass

    def set_deadline(self, deadline, clock=None):
        self.deadline = deadline

    def request_hd_photos(self, photo_ids):
        type(self).requested.extend(photo_ids)
        return {"accepted": len(photo_ids)}


class HdRequestWorkerTests(unittest.TestCase):
    def test_worker_drains_bounded_legacy_queue(self):
        FakeReveal.requested = []
        catalog = FakeCatalog()
        result = run_worker(
            {
                "TACTACAM_USERNAME": "owner@example.com",
                "TACTACAM_PASSWORD": "secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "service-secret",
            },
            max_requests=10,
            catalog_factory=lambda *_: catalog,
            reveal_factory=FakeReveal,
        )

        self.assertEqual(result, {"ok": True, "submitted": 2, "failed": 0, "unknown": 0, "empty": True})
        self.assertEqual(FakeReveal.requested, ["photo-1", "photo-2"])
        self.assertEqual(catalog.completed, ["token-1", "token-2"])

    def test_worker_does_not_retry_an_ambiguous_provider_side_effect(self):
        class TimingOutReveal(FakeReveal):
            def request_hd_photos(self, photo_ids):
                raise RevealError("response lost")

        catalog = FakeCatalog()
        catalog.claims = [catalog.claims[0], {"ok": True, "empty": True}]
        result = run_worker(
            {
                "TACTACAM_USERNAME": "owner@example.com",
                "TACTACAM_PASSWORD": "secret",
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "service-secret",
            },
            max_requests=10,
            catalog_factory=lambda *_: catalog,
            reveal_factory=TimingOutReveal,
        )

        self.assertEqual(result["unknown"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(catalog.unknown, [("token-1", "provider_outcome_unknown")])
        self.assertEqual(catalog.failed, [])


if __name__ == "__main__":
    unittest.main()
