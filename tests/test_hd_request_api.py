import unittest
from unittest.mock import patch

from api.hd_requests import _limit_from_path
from reveal_downloader.hd_request_worker import handle_hd_queue


class HdRequestApiTests(unittest.TestCase):
    def test_limit_is_bounded(self):
        self.assertEqual(_limit_from_path("/api/hd_requests?limit=12"), 12)
        for path in ("/api/hd_requests?limit=0", "/api/hd_requests?limit=21", "/api/hd_requests?limit=nope"):
            with self.assertRaises(ValueError):
                _limit_from_path(path)

    def test_requires_cron_bearer_before_running_worker(self):
        environment = {"CRON_SECRET": "0123456789abcdef"}
        status, payload = handle_hd_queue(environment, "Bearer wrong")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_authorized_request_runs_bounded_vercel_worker(self):
        environment = {"CRON_SECRET": "0123456789abcdef"}
        result = {"ok": True, "claimed": 2, "submitted": 2, "failed": 0, "unknown": 0}
        with patch("reveal_downloader.hd_request_worker.run_worker", return_value=result) as worker:
            status, payload = handle_hd_queue(
                environment, "Bearer 0123456789abcdef", max_requests=2
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, result)
        worker.assert_called_once_with(environment, max_requests=2, deadline_seconds=50.0)


if __name__ == "__main__":
    unittest.main()
