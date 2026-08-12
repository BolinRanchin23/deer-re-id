#!/usr/bin/env python3
"""Drain automatic HD requests, then analyze returned HD assets locally."""
import fcntl,json,os,subprocess,sys
from pathlib import Path
PROJECT_REF="vypmpmlhuqwvrxypowqa"; REPO=Path(__file__).resolve().parents[1]
def main():
 lock=open("/tmp/deerid-hd-pipeline.lock","w")
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:return 0
 try:
  keys=subprocess.run(["supabase","projects","api-keys","--project-ref",PROJECT_REF,"--output","json"],cwd=REPO,check=True,capture_output=True,text=True,timeout=30)
  key=next(x["api_key"] for x in json.loads(keys.stdout) if x.get("name")=="service_role" and x.get("api_key"))
  prod=REPO/".vercel/.env.production.local"
  if not prod.exists(): subprocess.run(["npx","--yes","vercel@latest","env","pull",str(prod),"--environment=production","--yes","--scope","deer-intel-pro"],cwd=REPO,check=True,capture_output=True,text=True,timeout=90)
  env={**os.environ,"SUPABASE_URL":f"https://{PROJECT_REF}.supabase.co","SUPABASE_SECRET_KEY":key,"SUPABASE_BUCKET":"tactacam-photos"}
  for line in prod.read_text().splitlines():
   if line.startswith(('TACTACAM_USERNAME=','TACTACAM_PASSWORD=')):
    name,value=line.split('=',1); env[name]=value.strip().strip("'\"").replace('\\n','\n')
  for module in ("reveal_downloader.hd_request_worker","scripts.run_hd_analysis_local"):
   result=subprocess.run([sys.executable,"-m",module],cwd=REPO,env=env,capture_output=True,text=True,timeout=1200)
   if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{module} failed")
  return 0
 except Exception as exc: print(f"DeerID HD pipeline failed: {exc}",file=sys.stderr);return 1
if __name__=="__main__":raise SystemExit(main())
