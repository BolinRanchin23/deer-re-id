#!/usr/bin/env python3
"""Quiet scheduler entry point for OpenAI-backed Gate 1B vision."""
import fcntl,json,os,subprocess,sys,time
from pathlib import Path
PROJECT_REF="vypmpmlhuqwvrxypowqa"; REPO=Path(__file__).resolve().parents[1]
BWS=Path.home()/".hermes/profiles/deer-id-dev-bot/bin/bws"
def _bitwarden_openai_key():
 for attempt in range(2):
  try:
   records=json.loads(subprocess.run([str(BWS),"secret","list","--output","json"],check=True,capture_output=True,text=True,timeout=30).stdout)
   return next(x["value"] for x in records if x.get("key")=="OPENAI_API_KEY" and x.get("value"))
  except (json.JSONDecodeError,StopIteration,subprocess.SubprocessError):
   if attempt: raise
   time.sleep(2)
def main():
 lock=open("/tmp/deerid-gate1b.lock","w")
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: return 0
 try:
  keys=subprocess.run(["supabase","projects","api-keys","--project-ref",PROJECT_REF,"--output","json"],cwd=REPO,check=True,capture_output=True,text=True,timeout=30)
  service_key=next(x["api_key"] for x in json.loads(keys.stdout) if x.get("name")=="service_role" and x.get("api_key"))
  openai_key=os.environ.get("OPENAI_API_KEY") or _bitwarden_openai_key()
  if not openai_key: raise RuntimeError("OPENAI_API_KEY is unavailable")
  env={**os.environ,"SUPABASE_URL":f"https://{PROJECT_REF}.supabase.co","SUPABASE_SECRET_KEY":service_key,"SUPABASE_BUCKET":"tactacam-photos","OPENAI_API_KEY":openai_key}
  done=subprocess.run([sys.executable,"-m","reveal_downloader.gate1b_worker","--limit","20"],cwd=REPO,env=env,check=True,capture_output=True,text=True,timeout=900)
  result=json.loads(done.stdout)
  if result.get("failed"): raise RuntimeError(f"Gate 1B left {result['failed']} event(s) pending after model failure")
  return 0
 except Exception as exc: print(f"DeerID Gate 1B scheduler failed: {exc}",file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
