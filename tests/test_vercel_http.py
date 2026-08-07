import unittest
from http.server import BaseHTTPRequestHandler

from api.sync import handler


class VercelHttpAdapterTests(unittest.TestCase):
    def test_handler_is_a_vercel_python_http_handler(self):
        self.assertTrue(issubclass(handler, BaseHTTPRequestHandler))


if __name__ == "__main__":
    unittest.main()
