"""Authenticated Vercel Cron endpoint for bounded Gate 1B inference."""
from http.server import BaseHTTPRequestHandler
import json,os
from reveal_downloader.gate1b_worker import handle_cron

class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status,payload=handle_cron(os.environ,self.headers.get("Authorization"))
        body=json.dumps(payload,sort_keys=True,separators=(",",":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
