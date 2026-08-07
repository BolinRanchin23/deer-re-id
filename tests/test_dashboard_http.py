import unittest
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path

from api.status import handler as StatusHandler
from api.preview import handler as PreviewHandler


class DashboardHttpAdapterTests(unittest.TestCase):
    def test_status_and_preview_are_vercel_python_handlers(self):
        self.assertTrue(issubclass(StatusHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(PreviewHandler, BaseHTTPRequestHandler))
        self.assertTrue(Path("api/preview.py").exists())

    def test_root_homepage_contains_operational_dashboard_shell(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn("DeerID Operations", html)
        self.assertIn("/api/status", html)
        self.assertIn("Recent runs", html)
        self.assertIn("Photo archive integrity", html)
        self.assertNotIn("SUPABASE_SECRET_KEY", html)
        self.assertNotIn("CRON_SECRET", html)
        self.assertNotIn("Fort McKavett", html)
        self.assertIn("Photo previews are temporarily enabled", html)
        self.assertIn("renderPreviews", html)
        self.assertNotIn("Archived photos are never served by this dashboard", html)
        self.assertNotIn('<span class="check">✓</span>', html)
        self.assertIn("Math.min(n(verified.image),n(verified.metadata),n(verified.checksum))", html)
        self.assertNotIn("n(run.downloaded), n(run.skipped), n(run.failed)", html)

    def test_vercel_config_sets_static_dashboard_security_headers_without_cron(self):
        config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
        self.assertNotIn("crons", config)
        self.assertIn("api/preview.py", config["functions"])
        root = next(item for item in config["headers"] if item["source"] == "/")
        headers = {item["key"]: item["value"] for item in root["headers"]}
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
