"""Authenticated DeerID photo library and exact camera-map endpoint."""
from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.auth import authenticate_session
from reveal_downloader.catalog import handle_library


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        auth_status, user, cookies = authenticate_session(
            os.environ, self.headers.get("Cookie")
        )
        if auth_status == 200 and user is not None:
            status, payload = handle_library(os.environ, True)
        else:
            status, payload = auth_status, {"ok": False, "error": "unauthorized"}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
