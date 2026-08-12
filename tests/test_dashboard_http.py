import json
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api.library import handler as LibraryHandler
from api.library_preview import handler as LibraryPreviewHandler
from api.profile_assignment import handler as ProfileAssignmentHandler
from api.review import handler as ReviewHandler
from api.status import handler as StatusHandler


class DashboardHttpAdapterTests(unittest.TestCase):
    def test_status_library_and_preview_are_vercel_python_handlers(self):
        self.assertTrue(issubclass(StatusHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(LibraryPreviewHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(ProfileAssignmentHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(ReviewHandler, BaseHTTPRequestHandler))

    def test_root_homepage_contains_operational_dashboard_shell(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn("DeerID Workspace", html)
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("/api/status", app_js)
        self.assertIn("Recent ingestion runs", html)
        self.assertIn("Archive integrity", html)
        self.assertNotIn("SUPABASE_SECRET_KEY", html)
        self.assertNotIn("CRON_SECRET", html)
        self.assertNotIn("Fort McKavett", html)
        self.assertNotIn("Recent photos", html)
        self.assertNotIn("renderPreviews", app_js)
        self.assertNotIn("Archived photos are never served by this dashboard", html)
        self.assertNotIn('<span class="check">✓</span>', html)
        compact_js = "".join(app_js.split())
        self.assertIn(
            "Math.min(n(verified.image),n(verified.metadata),n(verified.checksum))",
            compact_js,
        )
        self.assertNotIn("n(run.downloaded), n(run.skipped), n(run.failed)", app_js)
        self.assertIn("Photo archive", html)
        self.assertIn("camera map", html)
        self.assertIn("Satellite", html)
        self.assertIn("/api/library", app_js)
        self.assertIn("server.arcgisonline.com", app_js)
        self.assertNotIn("/api/auth", app_js)
        self.assertNotIn("Sign in", html)
        self.assertNotIn("Forgot password", html)
        for view in ("Overview", "Review", "Deer", "Cameras", "Photos"):
            self.assertIn(view, html)
        self.assertIn('id="workspace-shell"', html)
        self.assertNotIn("sessionStorage", html + app_js)
        self.assertNotIn("<script>", html)
        self.assertIn('<script src="/app.js?v=15" defer></script>', html)
        self.assertIn('id="photos-24h"', html)
        self.assertIn('id="hd-requests-24h"', html)
        self.assertIn('id="hd-available-24h"', html)
        self.assertIn("stats.photos_received_24h", app_js)
        self.assertIn("stats.hd_requests_24h", app_js)
        self.assertNotIn("GOOGLE_MAPS_BROWSER_KEY", html)

    def test_mobile_review_places_consolidated_quick_actions_beside_the_photo(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        compact_js = "".join(app_js.split())
        self.assertIn("card.append(image,quickActions,meta)", compact_js)
        self.assertIn("Pass → Request HD", app_js)
        self.assertNotIn("Keep for ID", app_js)
        self.assertIn("Create new deer profile", app_js)
        self.assertIn("Add photo to profile", app_js)
        self.assertIn("/api/profile_assignment", app_js)
        compact_html = "".join(html.split())
        self.assertIn("grid-template-columns:repeat(3,1fr)", compact_html)
        self.assertIn(".profile-create-rowinput,.profile-create-rowbutton{grid-column:1/-1", compact_html)

    def test_returned_hd_review_is_scoped_to_one_animal_instance(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        app_js = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("Animal ${item.instance_index} of ${item.instance_count}", app_js)
        self.assertIn("hd-instance-crop", app_js)
        self.assertIn("hd-context-box", app_js)
        self.assertIn("item.hd_animal_instance_id", app_js)
        self.assertIn("One deer at a time", app_js)
        self.assertIn(".hd-context-box", html)

    def test_vercel_config_sets_static_dashboard_security_headers_and_only_gate1b_cron(self):
        config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["crons"],[{"path":"/api/gate1b_cron","schedule":"*/15 * * * *"}])
        self.assertIn("api/library.py", config["functions"])
        self.assertIn("api/library_preview.py", config["functions"])
        self.assertIn("api/profile_assignment.py", config["functions"])
        self.assertIn("api/review.py", config["functions"])
        self.assertNotIn("api/auth.py", config["functions"])
        root = next(item for item in config["headers"] if item["source"] == "/")
        headers = {item["key"]: item["value"] for item in root["headers"]}
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("server.arcgisonline.com", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(
            "'unsafe-inline'", headers["Content-Security-Policy"].split("style-src")[0]
        )
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
