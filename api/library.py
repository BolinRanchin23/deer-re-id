"""Open-prototype DeerID photo library and exact camera-map endpoint."""
from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.catalog import handle_library


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, payload = handle_library(os.environ)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
