"""Record a human decision from returned-HD profile review."""
import json, os
from http.server import BaseHTTPRequestHandler
from reveal_downloader.catalog import handle_hd_review_decision
MAX_BODY_BYTES=4096
class handler(BaseHTTPRequestHandler):
 def do_POST(self):
  try:
   length=int(self.headers.get("Content-Length","0")); payload=json.loads(self.rfile.read(length).decode()) if 0<length<=MAX_BODY_BYTES else None
  except (ValueError,UnicodeDecodeError,json.JSONDecodeError): payload=None
  if not isinstance(payload,dict): return self._write(400,{"ok":False,"error":"invalid request"})
  status,result=handle_hd_review_decision(os.environ,payload.get("action_token") or "",payload.get("action","") or "",instance_id=payload.get("hd_animal_instance_id","") or "",profile_id=payload.get("profile_id","") or "",display_name=payload.get("display_name","") or "",species=payload.get("species","") or "",sex=payload.get("sex","") or "",note=payload.get("note","") or ""); self._write(status,result)
 def _write(self,status,payload):
  body=json.dumps(payload,sort_keys=True,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
