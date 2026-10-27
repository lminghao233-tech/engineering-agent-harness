import hmac
import json
import os
import platform
import re
import sqlite3
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .store import Store, decode, uid
from .workspace import Workspace, sha


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(min_length=3, max_length=120, pattern=r"^[\w.-]+$")
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    problem_statement: str = Field(min_length=10, max_length=30000)
    tests: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=20)
    max_calls: int = Field(default=12, ge=1, le=100)
    max_tokens: int = Field(default=240000, ge=4096, le=2000000)
    max_seconds: int = Field(default=1800, ge=10, le=7200)
    max_tool_seconds: int = Field(default=600, ge=1, le=3600)
    data_revision: str = Field(default="manual", max_length=100)

    @field_validator("tests")
    @classmethod
    def test_paths(cls, value):
        for item in value:
            if not re.fullmatch(r"[\w./:\[\]-]+", item) or item.startswith(("-", "/")) or ".." in item:
                raise ValueError("relative test paths/selectors required")
        return value


class ReviewInput(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=4000)


class ExperimentInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    instance_ids: list[str] = Field(min_length=1, max_length=30)
    max_calls: int = Field(default=12, ge=1, le=100)
    max_tokens: int = Field(default=240000, ge=4096, le=2000000)


def make_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.root, (settings.key, settings.token))
    app = FastAPI(title="Engineering Agent Harness", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.store = store

    async def auth(request: Request):
        supplied = request.headers.get("Authorization", "")
        if len(settings.token) < 32 or not hmac.compare_digest(supplied, "Bearer " + settings.token):
            raise HTTPException(401, "Configure HARNESS_API_TOKEN (>=32 chars) and enter it in the console")

    @app.middleware("http")
    async def guards(request, call_next):
        if int(request.headers.get("content-length", "0")) > 256000:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "record not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": "0.1.0", "database": "ready"}

    @app.get("/api/system", dependencies=[Depends(auth)])
    def system():
        return {"blockers": settings.blockers(), "model": settings.model or None, "image": settings.image or None,
                "model_calls_authorized": settings.authorized, "python": platform.python_version(),
                "scope": "single trusted team; Linux Docker execution; local review only",
                "official_evaluation": "not executed by console; use trusted scripts/official_eval.py",
                "pricing": {"cost": None, "source": "unconfigured"}}

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    def tasks(state: str | None = None):
        return [t for t in store.tasks() if not state or t["state"] == state]

    @app.post("/api/tasks", status_code=201, dependencies=[Depends(auth)])
    def create(spec: TaskInput):
        return store.task(store.create({**spec.model_dump(), "execution_config": execution_config()}))

    def execution_config():
        return {"model": settings.model, "image": settings.image, "context_tokens": settings.context_tokens}

    @app.get("/api/tasks/{task}", dependencies=[Depends(auth)])
    def detail(task: str):
        row = store.task(task)
        return {**row, **{table: store.rows(table, task) for table in ("events", "attempts", "checkpoints", "artifacts", "reviews")}}

    @app.get("/api/tasks/{task}/events", dependencies=[Depends(auth)])
    def events(task: str, after: int = 0):
        store.task(task)
        return [e for e in store.rows("events", task) if e["seq"] > after]

    @app.get("/api/tasks/{task}/stream", dependencies=[Depends(auth)])
    async def stream(task: str, after: int = 0):
        import asyncio
        store.task(task)

        async def generate():
            nonlocal after
            for _ in range(120):
                for event in store.rows("events", task):
                    if event["seq"] > after:
                        after = event["seq"]
                        yield f"id: {after}\ndata: {json.dumps(event)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/tasks/{task}/cancel", dependencies=[Depends(auth)])
    def cancel(task: str):
        store.cancel(task)
        return store.task(task)

    @app.post("/api/tasks/{task}/resume", dependencies=[Depends(auth)])
    def resume(task: str):
        with store.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            if row["state"] not in {"BLOCKED", "FAILED", "CANCELLED"}:
                raise ValueError("only blocked/failed/cancelled tasks can resume")
            if row["owner"] or (row["lease_until"] or 0) > time.time():
                raise ValueError("worker must release or expire before resume")
            if row.get("arm") == "baseline":
                raise ValueError("baseline arm has recovery disabled; create a new experimental run")
            spec = row["spec"]
            if row["calls"] >= spec["max_calls"] or row["tokens"] >= spec["max_tokens"] or row["elapsed"] >= spec["max_seconds"]:
                raise ValueError("remaining budget exhausted; budget is never reset on resume")
            db.execute("UPDATE tasks SET state='QUEUED',cancel=0,error=NULL,updated=? WHERE id=?", (time.time(), task))
            store.event(db, task, "resume_requested", {"previous_state": row["state"], "budgets_preserved": True})
        return store.task(task)

    @app.post("/api/tasks/{task}/review", dependencies=[Depends(auth)])
    def review(task: str, review: ReviewInput):
        with store.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            if row["state"] != "AWAITING_REVIEW":
                raise ValueError("task is not awaiting review")
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)", (uid(), task, time.time(), review.decision, review.reviewer, review.note))
            db.execute("UPDATE tasks SET state=?,updated=? WHERE id=?", (review.decision, time.time(), task))
            store.event(db, task, "review", {**review.model_dump(), "external_merge": False})
        return store.task(task)

    @app.get("/api/artifacts/{artifact}", dependencies=[Depends(auth)])
    def artifact(artifact: str):
        with store.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact,)).fetchone()
        if row is None:
            raise KeyError(artifact)
        path = (store.root / row["path"]).resolve()
        if not path.is_relative_to(store.root.resolve()) or sha(path.read_bytes()) != row["sha256"]:
            raise HTTPException(409, "artifact integrity failure")
        return FileResponse(path, filename=f"{row['task_id']}-{row['kind']}.{'patch' if row['kind'] == 'patch' else 'json'}", media_type="text/plain")

    @app.get("/api/catalog", dependencies=[Depends(auth)])
    def catalog():
        path = Path(__file__).parent.parent / "data" / "agent-visible" / "tasks.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    @app.get("/api/manifest", dependencies=[Depends(auth)])
    def manifest():
        path = Path(__file__).parent.parent / "data" / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_downloaded", "instructions": "python scripts/prepare_data.py"}

    @app.post("/api/experiments", status_code=201, dependencies=[Depends(auth)])
    def experiment(data: ExperimentInput):
        catalog_rows = {r["instance_id"]: r for r in catalog()}
        frozen = manifest()
        ids = data.instance_ids
        if len(ids) != len(set(ids)) or not set(ids) <= set(frozen.get("selected_test_ids", [])):
            raise ValueError("experiment must use unique IDs from the frozen test manifest")
        if not set(ids) <= catalog_rows.keys():
            raise ValueError("download the pinned dataset first")
        eid = uid()
        with store.transaction() as db:
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?)", (eid, time.time(), data.name, json.dumps({"ids": ids, "dataset": frozen}), json.dumps(data.model_dump())))
            for arm in ("baseline", "enhanced"):
                for instance in ids:
                    row = catalog_rows[instance]
                    spec = TaskInput(**{k: row[k] for k in ("instance_id", "repo", "base_commit", "problem_statement", "data_revision")}, max_calls=data.max_calls, max_tokens=data.max_tokens).model_dump()
                    spec["execution_config"] = execution_config()
                    store.create(spec, eid, arm, db)
        return {"id": eid, "tasks": len(ids) * 2, "official_evaluation": "pending"}

    @app.get("/api/experiments", dependencies=[Depends(auth)])
    def experiments():
        from .metrics import summarize
        with store.connect() as db:
            rows = [decode(r) for r in db.execute("SELECT * FROM experiments ORDER BY created DESC")]
        all_tasks = store.tasks()
        return [{**r, "groups": {arm: summarize([t for t in all_tasks if t["experiment"] == r["id"] and t["arm"] == arm], len(r["manifest"]["ids"])) for arm in ("baseline", "enhanced")}} for r in rows]

    @app.get("/api/export", dependencies=[Depends(auth)])
    def export():
        return {"generated": time.time(), "experiments": experiments(), "tasks": store.tasks(), "note": "Null official_resolved means not officially evaluated; cost unavailable without pricing and usage."}

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="console")
    return app


app = make_app()
