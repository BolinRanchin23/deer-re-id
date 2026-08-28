"""Append a stale-safe corrected returned-HD animal crop box."""

import json
import os
from http.server import BaseHTTPRequestHandler

from reveal_downloader.catalog import handle_hd_geometry_correction

MAX_BODY_BYTES = 4096


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode()) if 0 < length <= MAX_BODY_BYTES else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("bbox"), dict):
            return self._write(400, {"ok": False, "error": "invalid request"})
        status, result = handle_hd_geometry_correction(
            os.environ,
            payload.get("action_token", ""),
            payload.get("hd_animal_instance_id", ""),
            payload.get("geometry_event_id"),
            payload["bbox"],
            payload.get("reason", ""),
            note=payload.get("note", ""),
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
