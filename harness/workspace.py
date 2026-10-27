import difflib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx

from .store import uid

MAX_FILE = 512_000
MAX_FILES = 40000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or any(ord(c) < 32 for c in relative):
        raise PermissionError("invalid relative POSIX path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or PureWindowsPath(relative).is_absolute() or any(p in {"..", ".git"} for p in rel.parts):
        raise PermissionError("path escapes workspace or accesses git history")
    for part in rel.parts:
        if part.endswith((".", " ")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
            raise PermissionError("non-portable or reserved path")
    root = Path(root).resolve()
    path = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise PermissionError("symlinks / junctions are forbidden")
    if not path.resolve().is_relative_to(root):
        raise PermissionError("path escapes workspace")
    return path


def inventory(root, limit_mb=256):
    files = {}
    size = 0
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(p in {".git", "__pycache__", ".pytest_cache"} for p in path.relative_to(root).parts):
            continue
        safe_path(root, rel)
        if path.is_file():
            size += path.stat().st_size
            if size > limit_mb * 1024 * 1024 or len(files) >= MAX_FILES:
                raise ValueError("workspace disk / file count limit exceeded")
            files[rel] = sha(path.read_bytes())
    return files


def extract_archive(content, destination, limit_mb=256):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_FILES:
            raise ValueError("invalid archive member count")
        prefixes = {m.filename.split("/")[0] for m in members}
        if len(prefixes) != 1:
            raise ValueError("archive needs one root directory")
        total = 0
        for member in members:
            rel = "/".join(member.filename.split("/")[1:])
            if not rel or member.is_dir():
                continue
            total += member.file_size
            if total > limit_mb * 1024 * 1024:
                raise ValueError("archive expansion limit exceeded")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PermissionError("repository contains symlink; operator review required")
            path = safe_path(destination, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(member))


def fetch_repo(spec, destination, limit_mb=256):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", spec["repo"]):
        raise ValueError("only public GitHub owner/repo is supported")
    if not re.fullmatch(r"[a-f0-9]{40}", spec["base_commit"]):
        raise ValueError("full base commit SHA required")
    url = f"https://codeload.github.com/{spec['repo']}/zip/{spec['base_commit']}"
    data = bytearray()
    with httpx.stream("GET", url, timeout=60, follow_redirects=False) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > limit_mb * 1024 * 1024:
                raise ValueError("repository download size limit exceeded")
    extract_archive(data, destination, limit_mb)
    return {"url": url, "sha256": sha(data), "fetched": time.time()}


def protected(path):
    p = PurePosixPath(path)
    return any(x in {"tests", "testing", ".github", ".git"} for x in p.parts) or p.name.startswith("test_") or p.name in {
            "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py", "sitecustomize.py", "usercustomize.py"
    }


class Workspace:
    def __init__(self, store, task, owner=None, fence=None, disk_mb=256, total_disk_mb=4096):
        self.store, self.task, self.owner, self.fence = store, task, owner, fence
        self.home = store.root / "tasks" / task
        self.base, self.repo = self.home / "base", self.home / "repo"
        self.disk_mb = disk_mb
        self.total_disk_mb = total_disk_mb
        self.trusted_tests = [p.split("::", 1)[0].rstrip("/") for p in self.store.task(task)["spec"].get("tests", [])]

    def is_protected(self, path):
        return protected(path) or any(t == "." or path == t or path.startswith(t + "/") for t in self.trusted_tests)

    def ensure_capacity(self, extra=0):
        task_size = sum(p.stat().st_size for p in self.home.rglob("*") if p.is_file()) if self.home.exists() else 0
        total = sum(p.stat().st_size for p in self.store.root.rglob("*") if p.is_file())
        if task_size + extra > self.disk_mb * 6 * 1024 * 1024:
            raise ToolErrorForDisk("per-task persisted disk budget exceeded")
        if total + extra > self.total_disk_mb * 1024 * 1024:
            raise ToolErrorForDisk("platform persisted disk budget exceeded")
        if shutil.disk_usage(self.store.root).free < extra + 64 * 1024 * 1024:
            raise ToolErrorForDisk("insufficient free disk; 64MiB reserve required")

    def assert_owner(self, db):
        if self.owner:
            row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (self.task,)).fetchone())
            self.store.assert_lease(row, self.owner, self.fence)

    def initialize(self, spec):
        self.home.mkdir(parents=True, exist_ok=True)
        if (self.home / "baseline.json").exists():
            expected = json.loads((self.home / "baseline.json").read_text())
            if inventory(self.base, self.disk_mb) != expected:
                raise ValueError("baseline integrity failure")
            return
        staging = self.home / f"prepare-{uid()}"
        self.ensure_capacity(2 * self.disk_mb * 1024 * 1024)
        source = fetch_repo(spec, staging, self.disk_mb)
        manifest = inventory(staging, self.disk_mb)
        if self.base.exists():
            raise ValueError("incomplete baseline; manual cleanup required")
        staging.rename(self.base)
        shutil.copytree(self.base, self.repo)
        (self.home / "baseline.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.store.record(self.task, "repository_prepared", source)

    def read(self, path):
        target = safe_path(self.repo, path)
        if target.stat().st_size > MAX_FILE:
            raise ValueError("file too large; use search / narrower file")
        return target.read_text(encoding="utf-8", errors="replace")

    def write(self, path, content, expected_hash, operation_id=None, crash_after_write=False):
        if self.is_protected(path):
            raise PermissionError("trusted test / configuration files are read-only; add reproduction under repro/")
        if len(content.encode()) > MAX_FILE:
            raise ValueError("file size limit")
        target = safe_path(self.repo, path)
        after = sha(content.encode())
        op = operation_id or uid()
        # Hold the DB write lock across the filesystem side effect. Another owner cannot
        # acquire a lease between the fencing check and replace. Journal intent is committed first.
        with self.store.transaction() as db:
            self.assert_owner(db)
            existing = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
            if existing:
                if (existing["task_id"], existing["path"], existing["after_hash"]) != (self.task, path, after):
                    raise ValueError("operation ID conflict")
                if existing["state"] == "CONFIRMED":
                    return {"operation_id": op, "state": "CONFIRMED", "sha256": after}
            else:
                current = sha(target.read_bytes()) if target.exists() else "ABSENT"
                if current != expected_hash:
                    raise ValueError("expected file hash mismatch; read file again")
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                           (op, self.task, path, current, after, content, "PENDING", time.time()))
        with self.store.transaction() as db:
            self.assert_owner(db)
            current = sha(target.read_bytes()) if target.exists() else "ABSENT"
            row = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
            if current == row["after_hash"]:
                pass
            elif current == row["before_hash"]:
                if sum(p.stat().st_size for p in self.repo.rglob("*") if p.is_file()) + len(content.encode()) > self.disk_mb * 1024 * 1024:
                    raise ValueError("workspace disk limit")
                self.ensure_capacity(len(content.encode()))
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".harness-" + uid())
                with temporary.open("wb") as stream:
                    stream.write(content.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            else:
                raise ValueError("uncertain operation: file matches neither expected hash")
            if crash_after_write:
                os._exit(91)  # deterministic fault-injection entry point, never model exposed
            db.execute("UPDATE operations SET state='CONFIRMED' WHERE id=?", (op,))
            self.store.event(db, self.task, "file_written", {"operation_id": op, "path": path, "sha256": after})
        return {"operation_id": op, "state": "CONFIRMED", "sha256": after}

    def reconcile(self):
        outcomes = []
        with self.store.transaction() as db:
            self.assert_owner(db)
            for row in db.execute("SELECT * FROM operations WHERE task_id=? AND state='PENDING'", (self.task,)).fetchall():
                target = safe_path(self.repo, row["path"])
                current = sha(target.read_bytes()) if target.exists() else "ABSENT"
                if current == row["after_hash"]:
                    state = "CONFIRMED"
                elif current == row["before_hash"]:
                    state = "NOT_APPLIED"
                else:
                    raise ValueError("uncertain side effect; operator reconciliation required")
                db.execute("UPDATE operations SET state=? WHERE id=?", (state, row["id"]))
                outcomes.append({"id": row["id"], "state": state})
            self.store.event(db, self.task, "operations_reconciled", outcomes)
        return outcomes

    def checkpoint(self, context, environment):
        manifest = inventory(self.repo, self.disk_mb)
        cp = uid()
        snapshot = self.home / "snapshots" / cp
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_capacity(sum(p.stat().st_size for p in self.repo.rglob("*") if p.is_file()))
        shutil.copytree(self.repo, snapshot, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        if inventory(snapshot, self.disk_mb) != manifest:
            raise ValueError("snapshot changed during capture")
        row = self.store.task(self.task)
        with self.store.transaction() as db:
            self.assert_owner(db)
            budget = {k: row[k] for k in ("calls", "tokens", "elapsed", "tool_seconds")}
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)", (cp, self.task, time.time(), json.dumps(manifest), json.dumps(context), json.dumps(budget), json.dumps(environment)))
            self.store.event(db, self.task, "checkpoint", {"id": cp, "files": len(manifest), "pending": []})
        # Retain two verifiable snapshots; all checkpoint metadata remains available for audit.
        for old in self.store.rows("checkpoints", self.task)[:-2]:
            old_path = self.home / "snapshots" / old["id"]
            if old_path.exists():
                self.discard(old_path)
        return cp

    def discard(self, path):
        resolved = path.resolve()
        if resolved == self.home.resolve() or not resolved.is_relative_to(self.home.resolve()) or path.is_symlink():
            raise PermissionError("refuse deletion outside task directory")
        shutil.rmtree(resolved)

    def restore(self):
        self.reconcile()
        cps = self.store.rows("checkpoints", self.task)
        if not cps:
            return []
        cp = cps[-1]
        snapshot = self.home / "snapshots" / cp["id"]
        if inventory(snapshot, self.disk_mb) != cp["manifest"]:
            raise ValueError("checkpoint integrity failure")
        # Preserve confirmed writes after the checkpoint, without re-executing them.
        current = inventory(self.repo, self.disk_mb)
        permitted = dict(cp["manifest"])
        for op in self.store.rows("operations", self.task):
            if op["state"] == "CONFIRMED" and op["created"] >= cp["created"]:
                permitted[op["path"]] = op["after_hash"]
        if current != permitted:
            # No process-memory restore: unknown test side effects are discarded only after archiving.
            archive = self.home / f"pre-restore-{uid()}"
            self.ensure_capacity(sum(p.stat().st_size for p in snapshot.rglob("*") if p.is_file()))
            self.repo.rename(archive)
            shutil.copytree(snapshot, self.repo)
            for op in self.store.rows("operations", self.task):
                if op["state"] == "CONFIRMED" and op["created"] >= cp["created"]:
                    target = safe_path(self.repo, op["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(op["content"], encoding="utf-8", newline="")
        self.store.record(self.task, "restored", {"checkpoint": cp["id"], "baseline": self.store.task(self.task)["spec"]["base_commit"], "confirmed_writes_preserved": True})
        return cp["context"]

    def diff(self):
        baseline = inventory(self.base, self.disk_mb)
        current = inventory(self.repo, self.disk_mb)
        chunks = []
        for name in sorted(set(baseline) | set(current)):
            if baseline.get(name) == current.get(name):
                continue
            if self.is_protected(name):
                raise PermissionError("trusted test / configuration was modified by task execution")
            before = safe_path(self.base, name)
            after = safe_path(self.repo, name)
            a = before.read_text(encoding="utf-8").splitlines(keepends=True) if before.exists() else []
            b = after.read_text(encoding="utf-8").splitlines(keepends=True) if after.exists() else []
            for lines in (a, b):
                if lines and not lines[-1].endswith("\n"):
                    raise ValueError("changed text files must end with newline for portable patch export")
            chunks.append(f"diff --git a/{name} b/{name}\n")
            chunks.extend(difflib.unified_diff(a, b, fromfile=f"a/{name}" if before.exists() else "/dev/null", tofile=f"b/{name}" if after.exists() else "/dev/null"))
        return "".join(chunks)

    def clean_verification(self):
        baseline = json.loads((self.home / "baseline.json").read_text())
        if inventory(self.base, self.disk_mb) != baseline:
            raise ValueError("baseline corrupted")
        self.diff()  # validates protected files and portable patch
        clean = self.home / f"verify-{uid()}"
        self.ensure_capacity(sum(p.stat().st_size for p in self.base.rglob("*") if p.is_file()) + sum(p.stat().st_size for p in self.repo.rglob("*") if p.is_file()))
        shutil.copytree(self.base, clean)
        current = inventory(self.repo, self.disk_mb)
        for name in sorted(set(baseline) | set(current)):
            if baseline.get(name) == current.get(name):
                continue
            dest = safe_path(clean, name)
            if name not in current:
                dest.unlink()
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe_path(self.repo, name), dest)
        return clean

    def artifact(self, kind, content):
        data = content.encode() if isinstance(content, str) else content
        self.ensure_capacity(len(data))
        aid = uid()
        path = self.home / "artifacts" / aid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with self.store.transaction() as db:
            self.assert_owner(db)
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)", (aid, self.task, kind, str(path.relative_to(self.store.root)), sha(data), time.time()))
        return aid


class ToolErrorForDisk(ValueError):
    """Capacity failures are blocked until storage changes; never retried as model errors."""
    category = "DISK_BUDGET"
