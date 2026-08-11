"""Human review action endpoint for model-selected Gate 1 photos."""

from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.catalog import handle_review

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
        status, result = handle_review(
            os.environ,
            payload.get("token", ""),
            payload.get("action", ""),
            payload.get("note", ""),
        )
        self._write(status, result)

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
