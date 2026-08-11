"""Secured Vercel endpoint for draining pre-pipeline HD requests."""

from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlsplit

from reveal_downloader.hd_request_worker import handle_hd_queue


def _limit_from_path(path: str) -> int:
    raw = parse_qs(urlsplit(path).query, keep_blank_values=True).get("limit", ["20"])
    if len(raw) != 1:
        raise ValueError("invalid limit")
    try:
        value = int(raw[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid limit") from exc
    if not 1 <= value <= 20:
        raise ValueError("invalid limit")
    return value


class handler(BaseHTTPRequestHandler):
    """Vercel handler for GET /api/hd_requests."""

    def do_GET(self) -> None:
        try:
            limit = _limit_from_path(self.path)
        except ValueError:
            self._write_json(400, {"ok": False, "error": "invalid limit"})
            return
        status, payload = handle_hd_queue(
            os.environ,
            self.headers.get("Authorization"),
            max_requests=limit,
        )
        self._write_json(status, payload)

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
