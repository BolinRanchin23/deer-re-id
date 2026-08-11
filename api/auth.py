"""Supabase email/password authentication endpoint."""
from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.auth import authenticate_session, handle_auth_action, valid_auth_request


_MAX_BODY = 16 * 1024


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, user, cookies = authenticate_session(os.environ, self.headers.get("Cookie"))
        payload = {"ok": True, "user": user} if status == 200 else {"ok": False, "error": "unauthorized"}
        self._send(status, payload, cookies)

    def do_POST(self) -> None:
        if not valid_auth_request(
            os.environ,
            origin=self.headers.get("Origin"),
            content_type=self.headers.get("Content-Type"),
            fetch_site=self.headers.get("Sec-Fetch-Site"),
        ):
            self._send(403, {"ok": False, "error": "forbidden"}, [])
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            self._send(400, {"ok": False, "error": "invalid request"}, [])
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid request"}, [])
            return
        if not isinstance(payload, dict):
            self._send(400, {"ok": False, "error": "invalid request"}, [])
            return
        status, response, cookies = handle_auth_action(os.environ, payload)
        self._send(status, response, cookies)

    def _send(self, status, payload, cookies) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
