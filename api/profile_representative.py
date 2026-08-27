"""Signed representative profile-photo selection."""
import json, os
from http.server import BaseHTTPRequestHandler
from reveal_downloader.catalog import handle_profile_representative
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length","0")); payload=json.loads(self.rfile.read(length)) if 0<length<=2048 else None
        except (ValueError,json.JSONDecodeError): payload=None
        if not isinstance(payload,dict): return self._write(400,{"ok":False,"error":"invalid request"})
        self._write(*handle_profile_representative(os.environ,payload.get("representative_token", ""),payload.get("profile_id", "")))
    def _write(self,status,payload):
        body=json.dumps(payload,sort_keys=True,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
