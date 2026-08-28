"""Apply append-only returned-HD animal topology corrections."""

import json
import os
from http.server import BaseHTTPRequestHandler

from reveal_downloader.catalog import handle_hd_instance_topology

MAX_BODY_BYTES = 8192


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode()) if 0 < length <= MAX_BODY_BYTES else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        required = {"action_token", "hd_animal_instance_id", "topology_event_id", "request_id", "action", "boxes"}
        allowed = required | {"note"}
        if not isinstance(payload, dict) or not required.issubset(payload) or not set(payload).issubset(allowed):
            return self._write(400, {"ok": False, "error": "invalid request"})
        status, result = handle_hd_instance_topology(
            os.environ,
            payload.get("action_token", ""),
            payload.get("hd_animal_instance_id", ""),
            payload.get("topology_event_id"),
            payload.get("request_id", ""),
            payload.get("action", ""),
            payload.get("boxes", []),
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
