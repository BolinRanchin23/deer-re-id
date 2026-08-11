import unittest
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path

from api.status import handler as StatusHandler
from api.library import handler as LibraryHandler
from api.library_preview import handler as LibraryPreviewHandler
from api.auth import handler as AuthHandler


class DashboardHttpAdapterTests(unittest.TestCase):
    def test_status_and_preview_are_vercel_python_handlers(self):
        self.assertTrue(issubclass(StatusHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryPreviewHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(AuthHandler, BaseHTTPRequestHandler))
        self.assertTrue(callable(getattr(AuthHandler, "do_GET", None)))
        self.assertTrue(callable(getattr(AuthHandler, "do_POST", None)))

    def test_root_homepage_contains_operational_dashboard_shell(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn("DeerID Operations", html)
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("/api/status", app_js)
        self.assertIn("Recent runs", html)
        self.assertIn("Photo archive integrity", html)
        self.assertNotIn("SUPABASE_SECRET_KEY", html)
        self.assertNotIn("CRON_SECRET", html)
        self.assertNotIn("Fort McKavett", html)
        self.assertNotIn("Recent photos", html)
        self.assertNotIn("renderPreviews", app_js)
        self.assertNotIn("Archived photos are never served by this dashboard", html)
        self.assertNotIn('<span class="check">✓</span>', html)
        compact_js = "".join(app_js.split())
        self.assertIn("Math.min(n(verified.image),n(verified.metadata),n(verified.checksum))", compact_js)
        self.assertNotIn("n(run.downloaded), n(run.skipped), n(run.failed)", app_js)
        self.assertIn("Private photo library", html)
        self.assertIn("Camera map", html)
        self.assertIn("Satellite", html)
        self.assertIn("/api/library", app_js)
        self.assertIn("/api/auth", app_js)
        self.assertNotIn("sessionStorage", html + app_js)
        self.assertNotIn("<script>", html)
        self.assertIn('<script src="/app.js" defer></script>', html)
        self.assertNotIn("GOOGLE_MAPS_BROWSER_KEY", html)

    def test_vercel_config_sets_static_dashboard_security_headers_without_cron(self):
        config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
        self.assertNotIn("crons", config)
        self.assertIn("api/library.py", config["functions"])
        self.assertIn("api/library_preview.py", config["functions"])
        self.assertIn("api/auth.py", config["functions"])
        root = next(item for item in config["headers"] if item["source"] == "/")
        headers = {item["key"]: item["value"] for item in root["headers"]}
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", headers["Content-Security-Policy"].split("style-src")[0])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
