"""Export actual persistent runtime evidence without credentials or benchmark answers."""
import json
import hashlib
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.config import Settings,redact
from harness.store import Store
from harness.metrics import summarize

settings=Settings()
store=Store(settings.root,(settings.key,settings.token))
out=ROOT/"evidence/runtime"
out.mkdir(parents=True,exist_ok=True)
tasks=store.tasks()
for task in tasks:
    # Restrict export to input provenance + execution evidence. No model key or gold material.
    safe_spec = dict(task["spec"])
    problem = safe_spec.pop("problem_statement", "")
    safe_spec["problem_statement_sha256"] = hashlib.sha256(problem.encode()).hexdigest()
    record={**task,"spec":safe_spec,**{table:store.rows(table,task["id"]) for table in ("events","attempts","checkpoints","artifacts","reviews")}}
    (out/(task["id"]+".json")).write_text(redact(json.dumps(record,ensure_ascii=False,indent=2),(settings.key,settings.token)),encoding="utf-8")
manifest=json.loads((ROOT/"data/manifest.json").read_text())
report={"kind":"real_public_issue_preflight_attempts","model_calls":sum(t["calls"] for t in tasks),
        "model_run_acceptance":"not accepted; no credential, no authorized model budget, no Linux Docker",
        "official_evaluation_acceptance":"not accepted",
        "groups":{arm:summarize([t for t in tasks if t["arm"]==arm],len(manifest["selected_test_ids"])) for arm in ("baseline","enhanced")},
        "blockers":settings.blockers(),"task_ids":[t["id"] for t in tasks]}
(ROOT/"evidence/benchmark-status.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"exported_tasks":len(tasks),"model_calls":report["model_calls"]}))
