"""Local supervisor: generates a local token, starts API/worker, terminates both on Ctrl+C."""
import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
parser=argparse.ArgumentParser()
parser.add_argument("--port",type=int,default=8787)
parser.add_argument("--no-worker",action="store_true")
args=parser.parse_args()
env_file=ROOT/".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key,value=line.split("=",1)
            os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
if not os.getenv("HARNESS_API_TOKEN"):
    token_path=ROOT/"var/access-token"
    token_path.parent.mkdir(exist_ok=True)
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(36))
        token_path.chmod(0o600)
    os.environ["HARNESS_API_TOKEN"]=token_path.read_text().strip()
print(f"Console: http://127.0.0.1:{args.port}",flush=True)
print("Access token: use HARNESS_API_TOKEN or read var/access-token; never commit it.",flush=True)
processes=[]
try:
    processes.append(subprocess.Popen([sys.executable,"-m","uvicorn","harness.api:app","--host","127.0.0.1","--port",str(args.port)]))
    if not args.no_worker:
        processes.append(subprocess.Popen([sys.executable,"-m","harness.worker"]))
    while all(p.poll() is None for p in processes):
        time.sleep(.5)
except KeyboardInterrupt:
    pass
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
