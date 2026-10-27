import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import redact

TRANSITIONS = {
    "QUEUED": {"PREPARING", "CANCELLED", "BLOCKED"},
    "PREPARING": {"RUNNING", "FAILED", "BLOCKED", "CANCELLED"},
    "RUNNING": {"VERIFYING", "FAILED", "BLOCKED", "CANCELLED"},
    "VERIFYING": {"AWAITING_REVIEW", "FAILED", "BLOCKED", "CANCELLED"},
    "AWAITING_REVIEW": {"APPROVED", "REJECTED"},
    "BLOCKED": {"QUEUED", "CANCELLED"},
    "FAILED": {"QUEUED"},
    "CANCELLED": {"QUEUED"},
    "REJECTED": set(), "APPROVED": set(),
}
ACTIVE = {"PREPARING", "RUNNING", "VERIFYING"}


def uid():
    return uuid.uuid4().hex


def decode(row):
    if row is None:
        raise KeyError("record not found")
    result = dict(row)
    for key in ("spec", "payload", "manifest", "context", "budget", "environment", "config"):
        if key in result:
            result[key] = json.loads(result[key])
    return result


class Store:
    def __init__(self, root: Path, secrets=()):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "harness.sqlite3"
        self.secrets = secrets
        with self.connect() as db:
            db.executescript((Path(__file__).parent / "migrations/001.sql").read_text())

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return contextlib.closing(db)

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def event(self, db, task, kind, payload):
        safe = redact(json.dumps(payload, ensure_ascii=False), self.secrets)
        if len(safe.encode()) > 160000:
            safe = json.dumps({"truncated": True, "summary": safe[:120000], "reason": "per-event output limit"})
        log_bytes = db.execute("SELECT coalesce(sum(length(payload)),0) FROM events WHERE task_id=?", (task,)).fetchone()[0]
        if log_bytes > 8_000_000 and kind not in {"state", "failure", "review", "official_evaluation"}:
            safe = json.dumps({"truncated": True, "reason": "task event log limit 8MB"})
        db.execute("INSERT INTO events(task_id,time,kind,payload) VALUES(?,?,?,?)", (task, time.time(), kind, safe))

    def create(self, spec, experiment=None, arm=None, db=None):
        if db is None:
            with self.transaction() as tx:
                return self.create(spec, experiment, arm, tx)
        task = uid()
        now = time.time()
        db.execute("INSERT INTO tasks(id,created,updated,state,spec,experiment,arm) VALUES(?,?,?,'QUEUED',?,?,?)",
                   (task, now, now, json.dumps(spec), experiment, arm))
        self.event(db, task, "created", {"source": spec.get("instance_id"), "arm": arm})
        return task

    def task(self, task):
        with self.connect() as db:
            return decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())

    def tasks(self):
        with self.connect() as db:
            return [decode(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 2000")]

    def rows(self, table, task):
        if table not in {"events", "attempts", "operations", "checkpoints", "artifacts", "reviews"}:
            raise ValueError("table not allowed")
        with self.connect() as db:
            return [decode(r) for r in db.execute(f"SELECT * FROM {table} WHERE task_id=? ORDER BY rowid", (task,))]

    def transition(self, task, target, error=None, owner=None, fence=None):
        with self.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            if owner is not None:
                self.assert_lease(row, owner, fence)
            if target not in TRANSITIONS.get(row["state"], set()):
                raise ValueError(f"Illegal transition {row['state']} -> {target}")
            db.execute("UPDATE tasks SET state=?,error=?,updated=? WHERE id=?",
                       (target, redact(error, self.secrets) if error else None, time.time(), task))
            self.event(db, task, "state", {"from": row["state"], "to": target, "error": error})

    @staticmethod
    def assert_lease(row, owner, fence):
        if row["owner"] != owner or row["fence"] != fence or (row["lease_until"] or 0) < time.time():
            raise RuntimeError("lease lost")

    def claim(self, owner, ttl):
        with self.transaction() as db:
            # Expired owners are fenced out; recovery is an explicit operator action.
            for row in db.execute("SELECT * FROM tasks WHERE state IN ('PREPARING','RUNNING','VERIFYING') AND lease_until < ?", (time.time(),)).fetchall():
                db.execute("UPDATE tasks SET state='BLOCKED',error='worker lease expired; reconcile and resume',owner=NULL,fence=fence+1 WHERE id=?", (row["id"],))
                db.execute("UPDATE attempts SET ended=?,outcome='LEASE_EXPIRED' WHERE task_id=? AND ended IS NULL", (time.time(), row["id"]))
                self.event(db, row["id"], "lease_expired", {"previous_owner": row["owner"]})
            if db.execute("SELECT 1 FROM tasks WHERE state IN ('PREPARING','RUNNING','VERIFYING') AND owner IS NOT NULL AND lease_until>=? LIMIT 1", (time.time(),)).fetchone():
                return None  # Global single-worker / single-container admission limit.
            row = db.execute("SELECT * FROM tasks WHERE state='QUEUED' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            fence = row["fence"] + 1
            now = time.time()
            db.execute("UPDATE tasks SET state='PREPARING',owner=?,fence=?,lease_until=?,updated=? WHERE id=?", (owner, fence, now + ttl, now, row["id"]))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,NULL,NULL)", (uid(), row["id"], fence, now))
            self.event(db, row["id"], "claimed", {"owner": owner, "fence": fence})
            return decode(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, task, owner, fence, ttl, seconds):
        exhausted = False
        with self.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            self.assert_lease(row, owner, fence)
            db.execute("UPDATE tasks SET lease_until=?,elapsed=elapsed+? WHERE id=?", (time.time() + ttl, seconds, task))
            if row["cancel"]:
                raise InterruptedError("cancel requested")
            if row["elapsed"] + seconds > row["spec"]["max_seconds"]:
                exhausted = True
        if exhausted:
            raise TimeoutError("task wall-clock budget exhausted")

    def reserve_call(self, task, owner, fence, reserve):
        with self.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            self.assert_lease(row, owner, fence)
            if row["cancel"]:
                raise InterruptedError("cancel requested")
            if row["calls"] >= row["spec"]["max_calls"] or row["tokens"] + reserve > row["spec"]["max_tokens"]:
                raise TimeoutError("model call / reserved token budget exhausted")
            db.execute("UPDATE tasks SET calls=calls+1,tokens=tokens+? WHERE id=?", (reserve, task))
            self.event(db, task, "model_reserved", {"tokens": reserve, "source": "conservative reservation; actual usage tracked separately"})

    def cancel(self, task):
        with self.transaction() as db:
            row = decode(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
            if row["state"] in {"QUEUED", "BLOCKED"}:
                db.execute("UPDATE tasks SET state='CANCELLED',cancel=1,updated=? WHERE id=?", (time.time(), task))
            elif row["state"] in ACTIVE:
                db.execute("UPDATE tasks SET cancel=1 WHERE id=?", (task,))
            else:
                raise ValueError("task is not cancellable")
            self.event(db, task, "cancel_requested", {})

    def release(self, task, owner, fence):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET ended=?,outcome=(SELECT state FROM tasks WHERE id=?) WHERE task_id=? AND fence=? AND ended IS NULL", (time.time(), task, task, fence))
            db.execute("UPDATE tasks SET owner=NULL,lease_until=NULL WHERE id=? AND owner=? AND fence=?", (task, owner, fence))

    def record(self, task, kind, payload):
        with self.transaction() as db:
            self.event(db, task, kind, payload)
