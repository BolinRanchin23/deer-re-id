"""Confirm or undo one pending returned-HD profile assignment."""

import json
import os
from http.server import BaseHTTPRequestHandler

from reveal_downloader.catalog import handle_hd_profile_assignment_review

MAX_BODY_BYTES = 2048


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode()) if 0 < length <= MAX_BODY_BYTES else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return self._write(400, {"ok": False, "error": "invalid request"})
        status, result = handle_hd_profile_assignment_review(
            os.environ,
            payload.get("proposal_token", ""),
            payload.get("proposal_id", ""),
            payload.get("action", ""),
        )
        self._write(status, result)

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
