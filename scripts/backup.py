"""Consistent SQLite backup. Stop worker first to include immutable artifact/snapshot files."""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.config import Settings

parser = argparse.ArgumentParser()
parser.add_argument("destination",type=Path)
args = parser.parse_args()
root = Settings().root
if args.destination.exists() or args.destination.resolve().is_relative_to(root):
    raise SystemExit("Destination must be new and outside live data directory")
with sqlite3.connect(root/"harness.sqlite3") as source:
    if source.execute("SELECT count(*) FROM tasks WHERE owner IS NOT NULL").fetchone()[0]:
        raise SystemExit("Stop worker and reconcile leases before backup")
    args.destination.mkdir(parents=True)
    with sqlite3.connect(args.destination/"harness.sqlite3") as target:
        source.backup(target)
for name in ("tasks","official"):
    if (root/name).exists():
        shutil.copytree(root/name,args.destination/name)
(args.destination/"backup.json").write_text(json.dumps({"source":str(root),"schema":1}))
print("Backup completed:",args.destination)
