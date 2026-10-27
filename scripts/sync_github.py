"""Create private repository and push using existing Git credential helper or GH_TOKEN.

Credentials remain in process memory / standard credential helper. Never persist or print tokens.
Run only after reviewing staged files; no force push, no token in remote URL.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument("--create-private",action="store_true")
parser.add_argument("--push",action="store_true")
args=parser.parse_args()
env={**os.environ,"GIT_TERMINAL_PROMPT":"0","GCM_INTERACTIVE":"never"}
token=os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
if not token:
    result=subprocess.run(["git","credential","fill"],input="protocol=https\nhost=github.com\n\n",text=True,capture_output=True,env=env,timeout=20)
    if result.returncode==0:
        values=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
        token=values.get("password")
if not token:
    print(json.dumps({"status":"blocked","reason":"No existing GitHub credential; sign in with Git Credential Manager or supply GH_TOKEN in environment. Never paste token into chat."}))
    raise SystemExit(2)


def request(path,method="GET",payload=None):
    req=urllib.request.Request("https://api.github.com"+path,data=json.dumps(payload).encode() if payload else None,method=method,
        headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"EngineeringAgentHarness"})
    with urllib.request.urlopen(req,timeout=30) as response:
        return json.load(response)


profile=request("/user")
if profile["login"]!="lminghao233-tech":
    raise SystemExit("Authenticated account differs from requested destination; no changes made")
name="engineering-agent-harness"
try:
    repo=request(f"/repos/{profile['login']}/{name}")
except urllib.error.HTTPError as exc:
    if exc.code!=404 or not args.create_private:
        raise SystemExit("Target repository unavailable; create it or pass --create-private")
    repo=request("/user/repos","POST",{"name":name,"private":True,"description":"Durable engineering agent execution, independent verification and evaluation evidence","auto_init":False})
print(json.dumps({"repository":repo["html_url"],"private":repo["private"]}))
if args.push:
    if not repo["private"]:
        raise SystemExit("Refusing to push to a public repository; private destination was requested")
    env["HARNESS_GIT_TOKEN"]=token
    helper='!"'+sys.executable.replace('\\','/')+'" "'+str(ROOT/"scripts/github_credential.py").replace('\\','/')+'"'
    subprocess.run(["git","-c","credential.helper=","-c","credential.helper="+helper,"push",repo["clone_url"],"main:main"],cwd=ROOT,env=env,check=True)
    print("Push completed; verify remote HEAD with git ls-remote.")
