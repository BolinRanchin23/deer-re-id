"""Create or extend a human-confirmed deer appearance profile from a review image."""

import json
import os
from http.server import BaseHTTPRequestHandler

from reveal_downloader.catalog import handle_profile_assignment

MAX_BODY_BYTES = 4096


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._write(400, {"ok": False, "error": "invalid request"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"ok": False, "error": "invalid request"})
            return
        if not isinstance(payload, dict):
            self._write(400, {"ok": False, "error": "invalid request"})
            return
        status, result = handle_profile_assignment(
            os.environ,
            payload.get("token", ""),
            payload.get("action", ""),
            display_name=payload.get("display_name", ""),
            species=payload.get("species", ""),
            sex=payload.get("sex", ""),
            profile_id=payload.get("profile_id", ""),
            notes=payload.get("notes", ""),
        )
        self._write(status, result)

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
