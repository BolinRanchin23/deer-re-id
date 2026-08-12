"""Authenticated event endpoint for one newly populated HD asset."""
from http.server import BaseHTTPRequestHandler
import json,os
from reveal_downloader.hd_analysis_worker import handle_trigger

MAX_BODY_BYTES=1024

def _asset_id_from_body(body:bytes)->str:
 if not body or len(body)>MAX_BODY_BYTES: raise ValueError("invalid body")
 try: value=json.loads(body.decode("utf-8"))
 except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise ValueError("invalid body") from exc
 if not isinstance(value,dict) or set(value)!={"media_asset_id"} or not isinstance(value["media_asset_id"],str): raise ValueError("invalid body")
 return value["media_asset_id"]

class handler(BaseHTTPRequestHandler):
 def do_POST(self)->None:
  try:
   length=int(self.headers.get("Content-Length","0"))
   if not 1<=length<=MAX_BODY_BYTES: raise ValueError("invalid body")
   asset_id=_asset_id_from_body(self.rfile.read(length))
  except (TypeError,ValueError): self._write_json(400,{"ok":False,"error":"invalid body"});return
  status,payload=handle_trigger(os.environ,self.headers.get("Authorization"),asset_id)
  self._write_json(status,payload)
 def _write_json(self,status:int,payload:dict)->None:
  body=json.dumps(payload,sort_keys=True).encode()
  self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
