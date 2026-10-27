"""Trusted evaluator entry point. Never callable by the model or mounted into its container."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.config import Settings
from harness.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--execute", action="store_true", help="operator authorization to consume local Docker resources")
    args = parser.parse_args()
    settings = Settings()
    store = Store(settings.root)
    task = store.task(args.task)
    patches = [a for a in store.rows("artifacts",args.task) if a["kind"] == "patch"]
    patch = (store.root / patches[-1]["path"]).read_text(encoding="utf-8") if patches else ""
    patch_hash = hashlib.sha256(patch.encode()).hexdigest()
    run_id = "harness-" + uuid.uuid4().hex + "-" + patch_hash[:12]
    destination = store.root / "official" / run_id
    destination.mkdir(parents=True)
    prediction = destination / "predictions.jsonl"
    prediction.write_text(json.dumps({"instance_id":task["spec"]["instance_id"],"model_name_or_path":settings.model or "unconfigured","model_patch":patch})+"\n",encoding="utf-8")
    dataset = ROOT / "data/evaluator-only/tasks.json"
    blockers = []
    if not shutil.which("docker"):
        blockers.append("Docker CLI missing")
    if platform.system() != "Linux":
        blockers.append("Run official evaluator on Linux/WSL2 worker")
    if not patch:
        blockers.append("No model-generated patch available")
    if not dataset.exists():
        blockers.append("Pinned evaluator dataset unavailable")
    try:
        version = importlib.metadata.version("swebench")
    except importlib.metadata.PackageNotFoundError:
        version = None
        blockers.append("Install pinned evaluator requirements in separate Python 3.12 environment")
    if not args.execute:
        blockers.append("--execute not supplied; local resource execution not authorized")
    command = [sys.executable,"-m","swebench.harness.run_evaluation","--dataset_name",str(dataset),"--predictions_path",str(prediction),"--instance_ids",task["spec"]["instance_id"],"--max_workers","1","--run_id",run_id]
    evidence = {"run_id":run_id,"task_id":args.task,"patch_sha256":patch_hash,"swebench_version":version,"time":time.time(),"command":command,"blockers":blockers,"executed":False,"resolved":None}
    if not blockers:
        with (destination / "stdout.log").open("w",encoding="utf-8") as stream:
            result = subprocess.run(command,cwd=destination,stdout=stream,stderr=subprocess.STDOUT,timeout=7200)
        evidence.update({"executed":True,"exit_code":result.returncode})
        # Only the report generated inside this unique run directory can be ingested.
        reports = list(destination.rglob("report.json"))
        instance = task["spec"]["instance_id"]
        for report in reports:
            payload = json.loads(report.read_text())
            entry = payload.get(instance)
            if isinstance(entry,dict) and isinstance(entry.get("resolved"),bool):
                evidence["resolved"] = entry["resolved"]
                evidence["report_path"] = str(report.relative_to(destination))
                evidence["report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
                with store.transaction() as db:
                    db.execute("UPDATE tasks SET official_resolved=?,official_run=? WHERE id=?",(int(entry["resolved"]),run_id,args.task))
                    store.event(db,args.task,"official_evaluation",evidence)
                break
    (destination / "attempt.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(evidence,ensure_ascii=False,indent=2))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
