#!/usr/bin/env python3
"""Quiet local scheduler entry point for returned-HD model review."""
import fcntl,json,os,subprocess,sys
from pathlib import Path
PROJECT_REF="vypmpmlhuqwvrxypowqa"; REPO=Path(__file__).resolve().parents[1]
def main():
 lock=open("/tmp/deerid-hd-analysis.lock","w")
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: return 0
 try:
  keys=subprocess.run(["supabase","projects","api-keys","--project-ref",PROJECT_REF,"--output","json"],cwd=REPO,check=True,capture_output=True,text=True,timeout=30)
  service_key=next(x["api_key"] for x in json.loads(keys.stdout) if x.get("name")=="service_role" and x.get("api_key"))
  env={**os.environ,"SUPABASE_URL":f"https://{PROJECT_REF}.supabase.co","SUPABASE_SECRET_KEY":service_key,"SUPABASE_BUCKET":"tactacam-photos","HD_OLLAMA_URL":"http://127.0.0.1:11434"}
  failures=0
  for _ in range(20):
   done=subprocess.run([sys.executable,"-m","reveal_downloader.hd_analysis_worker"],cwd=REPO,env=env,check=False,capture_output=True,text=True,timeout=900)
   if done.returncode: raise RuntimeError(done.stderr.strip() or "HD worker failed")
   result=json.loads(done.stdout)
   if result.get("empty"): break
   failures+=int(result.get("failed",0))
  if failures: raise RuntimeError(f"returned-HD worker recorded {failures} failure(s)")
  return 0
 except Exception as exc:
  print(f"DeerID returned-HD scheduler failed: {exc}",file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
