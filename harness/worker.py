import argparse
import json
import subprocess
import threading
import time

from .config import Settings, redact
from .model import Model, PROMPT_VERSION, compact
from .sandbox import DockerSandbox, ToolError
from .store import Store, uid
from .workspace import ToolErrorForDisk, Workspace, inventory, safe_path, sha


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.root, (settings.key, settings.token))
        self.owner = uid()

    def execute(self, task):
        tid, fence = task["id"], task["fence"]
        spec = task["spec"]
        last = time.monotonic()
        stop = None
        lock = threading.RLock()
        completed = threading.Event()

        def pulse(force=False):
            nonlocal last, stop
            with lock:
                now = time.monotonic()
                if stop:
                    raise stop
                if force or now - last > 0.5:
                    try:
                        self.store.heartbeat(tid, self.owner, fence, self.settings.lease_seconds, now - last)
                    except Exception as exc:
                        stop = exc
                        raise
                    last = now

        def keepalive():
            while not completed.wait(.5):
                try:
                    pulse()
                except Exception:
                    return

        watchdog = threading.Thread(target=keepalive, daemon=True)
        watchdog.start()

        sandbox = DockerSandbox(self.settings, tid, fence, pulse)
        workspace = Workspace(self.store, tid, self.owner, fence, self.settings.disk_mb, self.settings.total_disk_mb)
        enhanced = task.get("arm") != "baseline"
        try:
            blockers = self.settings.blockers()
            if blockers:
                raise ToolError("CONFIGURATION", "; ".join(blockers))
            actual_config = {"model": self.settings.model, "image": self.settings.image, "context_tokens": self.settings.context_tokens}
            expected_config = spec.get("execution_config", {})
            if any(expected_config.get(k) and expected_config[k] != v for k, v in actual_config.items()):
                raise ToolError("CONFIGURATION", "model/image/context configuration changed since task submission")
            if not expected_config.get("model"):
                spec["execution_config"] = actual_config
                with self.store.transaction() as db:
                    self.store.assert_lease(dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()), self.owner, fence)
                    db.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), tid))
            sandbox.cleanup()
            environment = sandbox.preflight()
            environment["limits"] = {"cpu": self.settings.cpu, "memory": self.settings.memory, "pids": 64, "workspace_mb": self.settings.disk_mb, "total_disk_mb": self.settings.total_disk_mb, "tool_timeout": self.settings.tool_timeout, "output_bytes": 128000, "network": "none"}
            pulse(True)
            from pathlib import Path
            source_root = Path(__file__).resolve().parents[1]
            try:
                revision = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or "uncommitted"
            except (OSError, subprocess.SubprocessError):
                revision = "unavailable"
            lockfile = source_root / "requirements.lock"
            self.store.record(tid, "environment", {**environment, "prompt_version": PROMPT_VERSION, "data_revision": spec.get("data_revision"), "hints_enabled": False,
                "code_revision": revision, "dependency_lock_sha256": sha(lockfile.read_bytes()) if lockfile.exists() else None,
                "source_sha256": sha(b"".join(p.read_bytes() for p in sorted((source_root / "harness").rglob("*.py"))))})
            self.store.record(tid, "timing", {"phase": "queue", "seconds": task["updated"] - task["created"]})
            start = time.monotonic()
            workspace.initialize(spec)
            pulse(True)
            self.store.record(tid, "timing", {"phase": "repository_preparation", "seconds": time.monotonic() - start})
            context = workspace.restore() if enhanced else []
            issue = json.dumps({k: spec[k] for k in ("repo", "base_commit", "problem_statement", "instance_id")})
            if not context:
                context = [{"role": "user", "content": issue}]
            self.store.transition(tid, "RUNNING", owner=self.owner, fence=fence)
            model = Model(self.settings, self.store, tid, self.owner, fence, pulse)
            if enhanced:
                workspace.checkpoint(context, environment)
            failures = 0
            while True:
                pulse(True)
                if enhanced and len(json.dumps(context).encode()) > self.settings.context_tokens - 2200:
                    context = compact(context, issue, "No history/network/test changes; trusted verifier required", self.settings.context_tokens - 3000)
                    self.store.record(tid, "context_compacted", {"messages": len(context), "method": "extractive-tail"})
                started = time.monotonic()
                try:
                    action = model.call(context, enhanced)
                    failures = 0
                except ToolError as exc:
                    if enhanced and exc.retryable and failures < 2:
                        failures += 1
                        self.store.record(tid, "retry", {"category": exc.category, "attempt": failures, "reservation_retained": True})
                        end = time.monotonic() + failures
                        while time.monotonic() < end:
                            pulse()
                            time.sleep(0.1)
                        continue
                    raise
                self.store.record(tid, "timing", {"phase": "model", "seconds": time.monotonic() - started})
                context.append({"role": "assistant", "content": json.dumps(action)})
                self.store.record(tid, "action", action)
                if action["tool"] == "finish":
                    break
                operation = uid()
                started = time.monotonic()
                try:
                    result = self.tool(workspace, sandbox, operation, action, spec)
                except ToolErrorForDisk:
                    raise
                except (ValueError, PermissionError, OSError, KeyError, TypeError) as exc:
                    result = {"error": str(exc), "category": "PERMISSION" if isinstance(exc, PermissionError) else "TOOL_INPUT"}
                except ToolError as exc:
                    result = {"error": str(exc), "category": exc.category}
                elapsed = time.monotonic() - started
                self.store.record(tid, "tool_result", {"operation_id": operation, "tool": action["tool"], "seconds": elapsed, "result": result})
                with self.store.transaction() as db:
                    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
                    self.store.assert_lease(row, self.owner, fence)
                    db.execute("UPDATE tasks SET tool_seconds=tool_seconds+? WHERE id=?", (elapsed, tid))
                    if row["tool_seconds"] + elapsed > spec["max_tool_seconds"]:
                        raise TimeoutError("total tool runtime budget exhausted")
                context.append({"role": "user", "content": "Tool evidence: " + json.dumps(result, ensure_ascii=False)[:10000]})
                if enhanced:
                    pulse(True)
                    workspace.checkpoint(context, environment)
            pulse(True)
            patch = workspace.diff()
            patch_id = workspace.artifact("patch", patch)
            if not patch:
                raise ToolError("EMPTY_PATCH", "Agent finished without a patch")
            self.store.transition(tid, "VERIFYING", owner=self.owner, fence=fence)
            clean = workspace.clean_verification()
            started = time.monotonic()
            verification = sandbox.run(clean, uid(), spec["tests"])
            verification.update({"kind": "independent_repository_tests", "official_swebench": False, "patch_sha256": sha(patch.encode()), "environment": environment, "original_tests_preserved": True})
            workspace.artifact("verification", redact(json.dumps(verification, ensure_ascii=False, indent=2), (self.settings.key, self.settings.token)))
            self.store.record(tid, "verification", verification)
            self.store.record(tid, "timing", {"phase": "verification", "seconds": time.monotonic() - started})
            workspace.artifact("report", json.dumps({"task_id": tid, "instance_id": spec["instance_id"], "patch_artifact": patch_id, "patch_sha256": sha(patch.encode()), "verification": verification, "official_resolved": None, "review_required": True}, ensure_ascii=False, indent=2))
            pulse(True)
            if verification["exit_code"] != 0:
                raise ToolError("TEST_ASSERTION", "independent tests failed; inspect verification artifact")
            self.store.transition(tid, "AWAITING_REVIEW", owner=self.owner, fence=fence)
        except InterruptedError as exc:
            self.store.transition(tid, "CANCELLED", str(exc), self.owner, fence)
        except (TimeoutError, ToolError, ValueError, PermissionError, OSError, RuntimeError, KeyError, TypeError) as exc:
            category = getattr(exc, "category", "BUDGET" if isinstance(exc, TimeoutError) else "EXECUTION")
            try:
                target = "BLOCKED" if category in {"CONFIGURATION", "ENVIRONMENT", "MODEL_REJECTED", "MODEL_TRANSPORT", "DISK_BUDGET"} else "FAILED"
                self.store.record(tid, "failure", {"category": category, "message": str(exc)})
                self.store.transition(tid, target, f"{category}: {exc}", self.owner, fence)
            except RuntimeError:
                pass  # A fenced worker cannot change the new owner's state.
        finally:
            completed.set()
            watchdog.join(timeout=2)
            sandbox.cleanup()
            self.store.release(tid, self.owner, fence)

    def tool(self, workspace, sandbox, operation, action, spec):
        tool, args = action["tool"], action["args"]
        if tool == "list_files":
            prefix = args.get("prefix", "")
            return {"files": [p for p in inventory(workspace.repo) if p.startswith(prefix)][:500]}
        if tool == "read_file":
            content = workspace.read(args["path"])
            return {"path": args["path"], "content": content[:16000], "truncated": len(content) > 16000, "sha256": sha(safe_path(workspace.repo, args["path"]).read_bytes())}
        if tool == "search":
            query = args["query"]
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise ValueError("query length must be 1..200")
            matches = []
            for path in inventory(workspace.repo):
                try:
                    for line, value in enumerate(workspace.read(path).splitlines(), 1):
                        if query.lower() in value.lower():
                            matches.append({"path": path, "line": line, "text": value[:300]})
                            if len(matches) >= 80:
                                return {"matches": matches, "truncated": True}
                except (ValueError, UnicodeError):
                    continue
            return {"matches": matches, "truncated": False}
        if tool == "write_file":
            return workspace.write(args["path"], args["content"], args["expected_hash"], operation)
        if tool == "run_tests":
            return {"kind": "exploratory", **sandbox.run(workspace.repo, operation, spec["tests"])}
        if tool == "diff":
            return {"diff": workspace.diff()[:20000]}
        raise PermissionError("tool not allowed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = Worker(Settings())
    while True:
        task = worker.store.claim(worker.owner, worker.settings.lease_seconds)
        if task:
            worker.execute(task)
        if args.once:
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
