import concurrent.futures
import json
import os
import subprocess
import sys
import time

import pytest

from harness.store import Store
from harness.workspace import Workspace, inventory, sha


def test_state_machine_and_review_separation(store,spec):
    tid = store.create(spec)
    with pytest.raises(ValueError):
        store.transition(tid,"APPROVED")
    row = store.claim("worker",30)
    store.transition(tid,"RUNNING",owner="worker",fence=row["fence"])
    store.transition(tid,"VERIFYING",owner="worker",fence=row["fence"])
    store.transition(tid,"AWAITING_REVIEW",owner="worker",fence=row["fence"])
    assert store.task(tid)["state"] == "AWAITING_REVIEW"


def test_concurrent_claim_has_one_owner(store,spec):
    store.create(spec)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda i:store.claim(str(i),30),range(8)))
    assert sum(row is not None for row in rows) == 1


def test_expired_owner_fenced_out(store,spec):
    tid = store.create(spec)
    row = store.claim("old",0.01)
    time.sleep(.03)
    assert store.claim("new",30) is None
    assert store.task(tid)["state"] == "BLOCKED"
    with pytest.raises(RuntimeError):
        store.transition(tid,"RUNNING",owner="old",fence=row["fence"])


def test_budget_reservation_survives_retry(store,spec):
    tid = store.create(spec)
    row = store.claim("w",30)
    store.reserve_call(tid,"w",row["fence"],60000)
    with pytest.raises(TimeoutError):
        store.reserve_call(tid,"w",row["fence"],60000)
    assert store.task(tid)["calls"] == 1
    assert store.task(tid)["tokens"] == 60000


def test_cancel_stops_heartbeat_and_no_more_calls(store,spec):
    tid=store.create(spec)
    row=store.claim("w",30)
    store.cancel(tid)
    with pytest.raises(InterruptedError):
        store.heartbeat(tid,"w",row["fence"],30,1)
    with pytest.raises(InterruptedError):
        store.reserve_call(tid,"w",row["fence"],100)


def test_checkpoint_integrity_and_clean_verifier(ready,store):
    tid,ws=ready
    ws.checkpoint([{"role":"user","content":"issue constraints"}],{"image":"fixture"})
    old=sha((ws.repo/"calc.py").read_bytes())
    ws.write("calc.py","def add(a, b):\n    return a + b\n",old)
    clean=ws.clean_verification()
    assert (clean/"calc.py").read_text().endswith("return a + b\n")
    assert (clean/"tests/test_calc.py").read_bytes()==(ws.base/"tests/test_calc.py").read_bytes()
    context=ws.restore()
    assert context[0]["content"]=="issue constraints"
    assert "return a + b" in (ws.repo/"calc.py").read_text()


def test_operation_id_is_idempotent(ready,store):
    tid,ws=ready
    content="def add(a, b):\n    return a + b\n"
    before=sha((ws.repo/"calc.py").read_bytes())
    first=ws.write("calc.py",content,before,"stable-operation")
    second=ws.write("calc.py",content,before,"stable-operation")
    assert first==second
    assert len(store.rows("operations",tid))==1
    with pytest.raises(ValueError):
        ws.write("calc.py","different\n",before,"stable-operation")


def test_real_worker_process_dies_after_write_before_ack_and_recovers(ready,store):
    tid,ws=ready
    ws.checkpoint([{"role":"user","content":"original issue"}],{"fixture":True})
    code = """
import sys
from pathlib import Path
from harness.store import Store
from harness.workspace import Workspace,sha
s=Store(Path(sys.argv[1]))
r=s.claim('crash-worker',.2)
w=Workspace(s,sys.argv[2],'crash-worker',r['fence'])
w.write('calc.py','def add(a, b):\\n    return a + b\\n',sha((w.repo/'calc.py').read_bytes()),'crash-operation',crash_after_write=True)
"""
    proc=subprocess.run([sys.executable,"-c",code,str(store.root),tid],env=os.environ.copy(),capture_output=True,timeout=20)
    assert proc.returncode==91,proc.stderr.decode(errors="replace")
    assert store.rows("operations",tid)[0]["state"]=="PENDING"
    assert "return a + b" in (ws.repo/"calc.py").read_text()
    time.sleep(.25)
    store.claim("reaper",30)
    assert store.task(tid)["state"]=="BLOCKED"
    store.transition(tid,"QUEUED")
    row=store.claim("recovered-worker",30)
    recovered=Workspace(store,tid,"recovered-worker",row["fence"])
    context=recovered.restore()
    assert context[0]["content"]=="original issue"
    assert store.rows("operations",tid)[0]["state"]=="CONFIRMED"
    assert len(store.rows("operations",tid))==1
    assert "return a + b" in (ws.repo/"calc.py").read_text()


def test_unknown_side_effect_is_not_replayed(ready,store):
    tid,ws=ready
    with store.transaction() as db:
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",("uncertain",tid,"calc.py","wrong-before","wrong-after","content","PENDING",time.time()))
    with pytest.raises(ValueError,match="uncertain"):
        ws.reconcile()


def test_tampered_snapshot_rejected(ready):
    tid,ws=ready
    cp=ws.checkpoint([],{})
    (ws.home/"snapshots"/cp/"calc.py").write_text("tamper")
    with pytest.raises(ValueError,match="integrity"):
        ws.restore()


def test_cancelled_queued_task_never_claimed(store,spec):
    tid=store.create(spec)
    store.cancel(tid)
    assert store.claim("w",30) is None


def test_elapsed_budget_enforced(store,spec):
    tid=store.create(spec)
    row=store.claim("w",30)
    with pytest.raises(TimeoutError):
        store.heartbeat(tid,"w",row["fence"],30,121)
