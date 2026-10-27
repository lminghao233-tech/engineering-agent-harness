import json
import re
import subprocess
import threading
import time


class ToolError(Exception):
    def __init__(self, category, message, retryable=False):
        super().__init__(message)
        self.category, self.retryable = category, retryable


class DockerSandbox:
    """Trusted broker; task processes see only a read-only source bind and bounded tmpfs."""
    def __init__(self, settings, task, fence, pulse):
        self.settings, self.task, self.fence, self.pulse = settings, task, fence, pulse
        self.image_info = None

    def preflight(self):
        try:
            result = subprocess.run(["docker", "info", "--format", "{{json .}}"], capture_output=True, timeout=15, check=True)
            info = json.loads(result.stdout)
            if info.get("OSType") != "linux":
                raise ToolError("ENVIRONMENT", "Linux Docker engine required")
            image = subprocess.run(["docker", "image", "inspect", self.settings.image], capture_output=True, timeout=15, check=True)
            self.image_info = json.loads(image.stdout)[0]
            return {"architecture": info.get("Architecture"), "memory": info.get("MemTotal"), "cpus": info.get("NCPU"), "image_id": self.image_info["Id"], "image_digests": self.image_info.get("RepoDigests", [])}
        except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ToolError("ENVIRONMENT", "Docker unavailable / prepared image not present: " + str(exc)) from exc

    def cleanup(self):
        # Includes orphaned executions from an expired fence; only this task's label is targeted.
        try:
            listing = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=harness.task={self.task}"], capture_output=True, timeout=10, check=True)
            for cid in listing.stdout.decode().split():
                if re.fullmatch(r"[a-f0-9]{12,64}", cid):
                    subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def command(self, workspace, operation, tests):
        if not re.fullmatch(r"[a-f0-9]{32}", operation):
            raise ValueError("invalid operation ID")
        for test in tests:
            if not re.fullmatch(r"[\w./:\[\]-]+", test) or test.startswith("-") or ".." in test or test.startswith("/"):
                raise PermissionError("test selectors must be relative paths; arbitrary commands/options forbidden")
        name = f"harness-{self.task[:12]}-{self.fence}-{operation[:12]}"
        return name, [
            "docker", "run", "--rm", "--name", name, "--label", f"harness.task={self.task}",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "65532:65532", "--pids-limit", "64", "--cpus", self.settings.cpu,
            "--memory", self.settings.memory, "--memory-swap", self.settings.memory,
            "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=67108864:67108864",
            "--log-driver", "none", "--stop-timeout", "1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "--tmpfs", f"/work:rw,nosuid,nodev,size={self.settings.disk_mb}m,mode=1777",
            "--mount", f"type=bind,src={workspace.resolve()},dst=/source,readonly",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--env", "HOME=/tmp", "--env", "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "--entrypoint", "/bin/sh", self.settings.image,
            "-c", 'cp -R /source/. /work/ && cd /work && exec "$@"', "harness",
            "/opt/venv/bin/python", "-I", "-c",
            "import sys, pytest; sys.path.insert(0, '/work'); raise SystemExit(pytest.main(sys.argv[1:]))",
            "-q", "-p", "no:cacheprovider", *tests,
        ]

    def run(self, workspace, operation, tests):
        from .workspace import safe_path
        # Broker's umask may be 0077. The bind root must be readable by the
        # distinct non-root container UID; private parent directories remain private.
        workspace.chmod(0o755)
        for path in workspace.rglob("*"):
            safe_path(workspace, path.relative_to(workspace).as_posix())
            path.chmod(0o755 if path.is_dir() else 0o644)
        name, command = self.command(workspace, operation, tests)
        start = time.monotonic()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = bytearray()
        too_large = threading.Event()

        def reader():
            while chunk := process.stdout.read(4096):
                remaining = 128_000 - len(output)
                output.extend(chunk[:max(0, remaining)])
                if len(chunk) > remaining:
                    too_large.set()
                    break

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            while process.poll() is None:
                self.pulse()
                if time.monotonic() - start > self.settings.tool_timeout:
                    raise ToolError("TOOL_TIMEOUT", "test execution time limit exceeded")
                if too_large.is_set():
                    raise ToolError("OUTPUT_LIMIT", "test output exceeds 128000 bytes")
                time.sleep(0.1)
            thread.join(timeout=2)
            result = {"exit_code": process.returncode, "output": output.decode(errors="replace"), "seconds": time.monotonic() - start, "container": name}
            if process.returncode in {125, 126, 127, 137}:
                raise ToolError("ENVIRONMENT", json.dumps(result))
            return result
        finally:
            if process.poll() is None:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                process.kill()
            process.wait(timeout=10)
            thread.join(timeout=2)
            process.stdout.close()
