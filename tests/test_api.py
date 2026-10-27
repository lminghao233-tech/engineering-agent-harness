import json

from harness.metrics import summarize
from harness.worker import Worker
from harness.config import Settings


def test_actual_api_create_events_cancel_resume(client,spec):
    created=client.post("/api/tasks",json=spec)
    assert created.status_code==201,created.text
    tid=created.json()["id"]
    assert client.get("/api/tasks").json()[0]["id"]==tid
    assert client.get(f"/api/tasks/{tid}/events").json()[0]["kind"]=="created"
    assert client.post(f"/api/tasks/{tid}/cancel").json()["state"]=="CANCELLED"
    assert client.post(f"/api/tasks/{tid}/resume").json()["state"]=="QUEUED"
    assert client.post(f"/api/tasks/{tid}/review",json={"decision":"APPROVED","reviewer":"operator","note":"review"}).status_code==409


def test_authentication_required(client):
    assert client.get("/api/tasks",headers={"Authorization":"Bearer wrong"}).status_code==401
    assert client.get("/health",headers={"Authorization":""}).status_code==200


def test_backend_frontend_static_and_csp(client):
    page=client.get("/")
    assert page.status_code==200 and "研发执行控制台" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/app.js").status_code==200
    assert client.get("/style.css").status_code==200


def test_answer_fields_rejected(client,spec):
    for field in ("patch","test_patch","FAIL_TO_PASS","eval_script","hints_text"):
        assert client.post("/api/tasks",json={**spec,field:"answer"}).status_code==422


def test_invalid_shell_or_repo_rejected(client,spec):
    assert client.post("/api/tasks",json={**spec,"repo":"https://internal/secret"}).status_code==422
    assert client.post("/api/tasks",json={**spec,"tests":["tests;curl evil"]}).status_code==422
    assert client.post("/api/tasks",json={**spec,"tests":["-pbadplugin"]}).status_code==422


def test_worker_without_credentials_records_blocker(store,spec):
    tid=store.create(spec)
    worker=Worker(Settings(root=store.root,token="x"*40,key="",model="",authorized=False))
    task=worker.store.claim(worker.owner,30)
    worker.execute(task)
    row=store.task(tid)
    assert row["state"]=="BLOCKED"
    assert row["calls"]==0
    assert "MODEL_NAME" in row["error"]
    assert store.rows("events",tid)[-1]["kind"]=="state"


def test_artifact_hash_integrity(client,ready):
    tid,ws=ready
    aid=ws.artifact("report",'{"valid":true}')
    assert client.get("/api/artifacts/"+aid).status_code==200
    (ws.home/"artifacts"/aid).write_text("tampered")
    assert client.get("/api/artifacts/"+aid).status_code==409


def test_unknown_official_results_never_counted_as_success(store,spec):
    tid=store.create(spec)
    summary=summarize([store.task(tid)],30)
    assert summary["official_resolved"] is None
    assert summary["end_to_end_resolved_rate"] is None
    assert summary["not_run"]==30


def test_malformed_instance_and_missing_record(client,spec):
    assert client.post("/api/tasks",json={**spec,"instance_id":"<script>alert(1)</script>"}).status_code==422
    assert client.get("/api/tasks/missing").status_code==404
