"""Run fixed-position engineering faults and retain machine-readable evidence, separate from benchmark scores."""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CASES={
 "write_before_ack":"test_real_worker_process_dies_after_write_before_ack_and_recovers",
 "worker_lease_expiry":"test_expired_owner_fenced_out",
 "tool_timeout":"test_tool_timeout_terminates_owned_subprocess",
 "model_rate_limit":"test_provider_rate_limit_is_retryable_and_consumes_budget",
 "cancellation":"test_cancel_stops_heartbeat_and_no_more_calls",
 "budget_exhaustion":"test_budget_reservation_survives_retry",
 "uncertain_operation":"test_unknown_side_effect_is_not_replayed",
}
parser=argparse.ArgumentParser()
parser.add_argument("--basetemp",type=Path)
args=parser.parse_args()
run_id=uuid.uuid4().hex
out=ROOT/"evidence/faults"/run_id
out.mkdir(parents=True)
xml=out/"junit.xml"
cmd=[sys.executable,"-m","pytest","-q","--tb=short","-k"," or ".join(CASES.values()),"--junitxml",str(xml)]
if args.basetemp:
    cmd.extend(["--basetemp",str(args.basetemp)])
start=time.monotonic()
result=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,timeout=120)
(out/"stdout.txt").write_text(result.stdout+result.stderr,encoding="utf-8")
tests={t.attrib["name"]:t for t in ET.parse(xml).iter("testcase")}
report={"run_id":run_id,"kind":"deterministic_engineering_fault_injection","official_swebench":False,
        "seed":20261027,"position_rule":"fixed by named test; identical input per repeat; not a real-model ablation",
        "command":cmd,"exit_code":result.returncode,"seconds":time.monotonic()-start,
        "cases":[{"fault":fault,"test":name,"passed":name in tests and not any(c.tag in {"failure","error","skipped"} for c in tests[name])} for fault,name in CASES.items()],
        "baseline_vs_enhanced_real_model":"not executed: model credential and Linux Docker worker unavailable"}
(ROOT/"evidence/fault-injection.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(result.returncode)
