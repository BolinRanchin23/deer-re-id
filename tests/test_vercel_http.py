import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import importlib
import json

from api.sync import _start_page_from_path, handler
from api.photos import handler as PhotosHandler
from api.profile_representative import handler as RepresentativeHandler


class VercelHttpAdapterTests(unittest.TestCase):
    def test_hd_review_queue_has_a_bounded_on_demand_http_adapter(self):
        self.assertTrue(Path("api/hd_review_queue.py").exists())
        module = importlib.import_module("api.hd_review_queue")
        self.assertTrue(issubclass(module.handler, BaseHTTPRequestHandler))
        self.assertTrue(Path("api/hd_review_workflow.py").exists())
        workflow_module = importlib.import_module("api.hd_review_workflow")
        self.assertTrue(issubclass(workflow_module.handler, BaseHTTPRequestHandler))
        self.assertTrue(Path("api/hd_profile_assignment_review.py").exists())
        buffer_module = importlib.import_module("api.hd_profile_assignment_review")
        self.assertTrue(issubclass(buffer_module.handler, BaseHTTPRequestHandler))
        self.assertTrue(Path("api/hd_geometry_correction.py").exists())
        geometry_module = importlib.import_module("api.hd_geometry_correction")
        self.assertTrue(issubclass(geometry_module.handler, BaseHTTPRequestHandler))
        config = json.loads(Path("vercel.json").read_text())
        self.assertEqual(config["functions"]["api/hd_review_queue.py"]["maxDuration"], 10)
        self.assertEqual(config["functions"]["api/hd_review_workflow.py"]["maxDuration"], 10)
        self.assertEqual(config["functions"]["api/hd_profile_assignment_review.py"]["maxDuration"], 10)
        self.assertEqual(config["functions"]["api/hd_geometry_correction.py"]["maxDuration"], 10)

    def test_hd_mutation_adapters_do_not_normalize_falsey_malformed_fields(self):
        workflow = Path("api/hd_review_workflow.py").read_text()
        geometry = Path("api/hd_geometry_correction.py").read_text()
        self.assertIn('payload.get("note", "")', workflow)
        self.assertIn('payload.get("note", "")', geometry)
        self.assertNotIn('payload.get("note") or ""', workflow)
        self.assertNotIn('payload.get("note") or ""', geometry)

    def test_refresh_handlers_are_vercel_http_handlers(self):
        self.assertTrue(issubclass(PhotosHandler, BaseHTTPRequestHandler))
        self.assertTrue(issubclass(RepresentativeHandler, BaseHTTPRequestHandler))

    def test_handler_is_a_vercel_python_http_handler(self):
        self.assertTrue(issubclass(handler, BaseHTTPRequestHandler))

    def test_start_page_is_read_from_query_string(self):
        self.assertEqual(_start_page_from_path("/api/sync?page=17"), 17)
        self.assertEqual(_start_page_from_path("/api/sync"), 0)

    def test_start_page_rejects_invalid_or_excessive_values(self):
        for path in ("/api/sync?page=-1", "/api/sync?page=abc", "/api/sync?page=1001"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _start_page_from_path(path)


if __name__ == "__main__":
    unittest.main()
