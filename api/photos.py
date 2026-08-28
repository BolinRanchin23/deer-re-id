"""Server-paged and filtered private photo archive."""
import json, os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from reveal_downloader.catalog import handle_photos_query
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query={key:values[-1] for key,values in parse_qs(urlsplit(self.path).query,keep_blank_values=True).items()}
        status,payload=handle_photos_query(os.environ,query)
        body=json.dumps(payload,sort_keys=True,separators=(",",":")).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
