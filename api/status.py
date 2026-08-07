"""Public, sanitized DeerID operational status endpoint."""
from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.dashboard import handle_status


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, payload = handle_status(os.environ)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
