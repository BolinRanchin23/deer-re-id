"""Short-lived proxy for authenticated DeerID library photos."""
from http.server import BaseHTTPRequestHandler
import os
from urllib.parse import parse_qs, urlsplit

from reveal_downloader.auth import authenticate_session
from reveal_downloader.catalog import handle_library_preview


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        auth_status, user, cookies = authenticate_session(
            os.environ, self.headers.get("Cookie")
        )
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        tokens = query.get("token", [])
        token = tokens[0] if len(tokens) == 1 else ""
        if auth_status == 200 and user is not None:
            status, content_type, body = handle_library_preview(os.environ, token)
        else:
            status, content_type, body = auth_status, "text/plain; charset=utf-8", b"Unauthorized"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
