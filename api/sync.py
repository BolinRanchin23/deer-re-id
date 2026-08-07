"""Secured Vercel Cron endpoint that archives Reveal photos to Supabase."""

from http.server import BaseHTTPRequestHandler
import json
import os

from reveal_downloader.vercel import handle_sync


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler for GET /api/sync."""

    def do_GET(self) -> None:
        status, payload = handle_sync(
            os.environ,
            self.headers.get("Authorization"),
        )
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
