"""Secured Vercel Cron endpoint that archives Reveal photos to Supabase."""

from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlsplit

from reveal_downloader.vercel import handle_sync


def _start_page_from_path(path: str) -> int:
    values = parse_qs(urlsplit(path).query, keep_blank_values=True).get("page", ["0"])
    if len(values) != 1:
        raise ValueError("invalid page")
    try:
        page = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid page") from exc
    if not 0 <= page <= 1000:
        raise ValueError("invalid page")
    return page


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler for GET /api/sync."""

    def do_GET(self) -> None:
        try:
            start_page = _start_page_from_path(self.path)
        except ValueError:
            self._write_json(400, {"ok": False, "error": "invalid page"})
            return
        status, payload = handle_sync(
            os.environ,
            self.headers.get("Authorization"),
            start_page=start_page,
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
