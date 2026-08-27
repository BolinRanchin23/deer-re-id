import unittest
from http.server import BaseHTTPRequestHandler

from api.sync import _start_page_from_path, handler
from api.photos import handler as PhotosHandler
from api.profile_representative import handler as RepresentativeHandler


class VercelHttpAdapterTests(unittest.TestCase):
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
