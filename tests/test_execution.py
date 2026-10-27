"""Deterministic engineering fixtures. These are never counted as real-model or SWE-bench results."""
import json
import os
import shutil
import subprocess
import sys
import time

import httpx
import pytest

from harness.config import Settings
from harness.model import Model
from harness.sandbox import DockerSandbox,ToolError
from harness.worker import Worker
from harness.workspace import sha


def test_full_worker_loop_independent_verifier_and_patch(ready,store,monkeypatch):
    tid,ws=ready
    settings=Settings(root=store.root,token="x"*40,key="fixture",model="deterministic-fixture",authorized=True,image="fixture@sha256:"+"a"*64)
    monkeypatch.setattr(Settings,"blockers",lambda self:[])
    monkeypatch.setattr(DockerSandbox,"preflight",lambda self:{"image":"deterministic-test-fixture","official":False})
    monkeypatch.setattr(DockerSandbox,"cleanup",lambda self:None)

    def run_fixture(self,workspace,operation,tests):
        # This source was authored in conftest.py, never supplied by user/model/network.
        code="import runpy; m=runpy.run_path('calc.py'); assert m['add'](2,3)==5"
        started=time.monotonic()
        result=subprocess.run([sys.executable,"-I","-c",code],cwd=workspace,capture_output=True,timeout=5)
        return {"exit_code":result.returncode,"output":result.stderr.decode(),"seconds":time.monotonic()-started,"fixture":True}

    monkeypatch.setattr(DockerSandbox,"run",run_fixture)
    actions=iter([
        {"tool":"write_file","args":{"path":"calc.py","content":"def add(a, b):\n    return a + b\n","expected_hash":sha((ws.repo/"calc.py").read_bytes())}},
        {"tool":"run_tests","args":{}},
        {"tool":"finish","args":{"summary":"deterministic engineering fixture only"}},
    ])

    def call(self,context,enhanced=True):
        self.store.reserve_call(self.task,self.owner,self.fence,1000)
        return next(actions)

    monkeypatch.setattr(Model,"call",call)
    worker=Worker(settings)
    worker.execute(store.claim(worker.owner,90))
    assert store.task(tid)["state"]=="AWAITING_REVIEW"
    assert store.task(tid)["calls"]==3
    assert store.task(tid)["official_resolved"] is None
    kinds={a["kind"] for a in store.rows("artifacts",tid)}
    assert kinds=={"patch","verification","report"}
    patch=[a for a in store.rows("artifacts",tid) if a["kind"]=="patch"][0]
    assert "+    return a + b" in (store.root/patch["path"]).read_text()
    if os.getenv("HARNESS_FIXTURE_EXPORT"):
        from pathlib import Path
        output=Path(os.environ["HARNESS_FIXTURE_EXPORT"])
        output.mkdir(parents=True,exist_ok=True)
        for artifact in store.rows("artifacts",tid):
            shutil.copy2(store.root/artifact["path"],output/(artifact["kind"]+(".patch" if artifact["kind"]=="patch" else ".json")))
        (output/"trace.json").write_text(json.dumps({"kind":"deterministic_engineering_fixture","real_model":False,"official_swebench":False,"task":store.task(tid),"events":store.rows("events",tid),"checkpoints":store.rows("checkpoints",tid)},ensure_ascii=False,indent=2),encoding="utf-8")


def test_model_adapter_records_actual_usage_and_never_refunds_reservation(store,spec,monkeypatch):
    tid=store.create(spec)
    row=store.claim("owner",90)
    settings=Settings(root=store.root,key="server-only-key",model="configured-model",context_tokens=4000)
    real_client=httpx.Client
    def handle(request):
        assert request.headers["Authorization"]=="Bearer server-only-key"
        body=json.loads(request.content)
        assert body["model"]=="configured-model"
        return httpx.Response(200,json={"model":"snapshot-123","usage":{"total_tokens":20},"choices":[{"message":{"content":json.dumps({"tool":"finish","args":{"summary":"done"}})}}]})
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    adapter=Model(settings,store,tid,"owner",row["fence"],lambda:None)
    assert adapter.call([{"role":"user","content":"issue"}])["tool"]=="finish"
    assert store.task(tid)["tokens"]==6048
    response=[e["payload"] for e in store.rows("events",tid) if e["kind"]=="model_response"][0]
    assert response["usage"]["total_tokens"]==20 and response["returned_model"]=="snapshot-123"
    assert response["cost"] is None


def test_provider_rate_limit_is_retryable_and_consumes_budget(store,spec,monkeypatch):
    tid=store.create(spec)
    row=store.claim("owner",90)
    settings=Settings(root=store.root,key="fixture",model="fixture",context_tokens=4000)
    real_client=httpx.Client
    monkeypatch.setattr(httpx,"Client",lambda **kwargs:real_client(transport=httpx.MockTransport(lambda r:httpx.Response(429)),**kwargs))
    adapter=Model(settings,store,tid,"owner",row["fence"],lambda:None)
    with pytest.raises(ToolError) as error:
        adapter.call([{"role":"user","content":"issue"}])
    assert error.value.category=="MODEL_RATE_LIMIT" and error.value.retryable
    assert store.task(tid)["calls"]==1


def test_tool_timeout_terminates_owned_subprocess(tmp_path,monkeypatch):
    settings=Settings(root=tmp_path,tool_timeout=0)
    runner=DockerSandbox(settings,"a"*32,1,lambda:None)
    monkeypatch.setattr(runner,"command",lambda *args:("owned-test-process",[sys.executable,"-c","import time; time.sleep(10)"]))
    original_run=subprocess.run
    monkeypatch.setattr(subprocess,"run",lambda *args,**kwargs:subprocess.CompletedProcess(args[0],0))
    started=time.monotonic()
    with pytest.raises(ToolError) as error:
        runner.run(tmp_path,"b"*32,["tests"])
    assert error.value.category=="TOOL_TIMEOUT"
    assert time.monotonic()-started<5


def test_global_admission_limits_container_concurrency_to_one(store,spec):
    store.create(spec)
    store.create({**spec,"instance_id":"second-task"})
    assert store.claim("first",90) is not None
    assert store.claim("second",90) is None
