import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    root: Path = field(default_factory=lambda: Path(os.getenv("HARNESS_DATA", "var")).resolve())
    token: str = field(default_factory=lambda: os.getenv("HARNESS_API_TOKEN", ""))
    endpoint: str = field(default_factory=lambda: os.getenv("MODEL_ENDPOINT", "https://api.openai.com/v1/chat/completions"))
    model: str = field(default_factory=lambda: os.getenv("MODEL_NAME", ""))
    key: str = field(default_factory=lambda: os.getenv("MODEL_API_KEY", ""))
    authorized: bool = field(default_factory=lambda: os.getenv("MODEL_CALLS_AUTHORIZED", "false").lower() == "true")
    image: str = field(default_factory=lambda: os.getenv("HARNESS_IMAGE", ""))
    cpu: str = field(default_factory=lambda: os.getenv("HARNESS_CPU", "2"))
    memory: str = field(default_factory=lambda: os.getenv("HARNESS_MEMORY", "2g"))
    disk_mb: int = field(default_factory=lambda: int(os.getenv("HARNESS_DISK_MB", "256")))
    total_disk_mb: int = field(default_factory=lambda: int(os.getenv("HARNESS_TOTAL_DISK_MB", "4096")))
    tool_timeout: int = field(default_factory=lambda: int(os.getenv("HARNESS_TOOL_TIMEOUT", "60")))
    lease_seconds: int = field(default_factory=lambda: int(os.getenv("HARNESS_LEASE_SECONDS", "90")))
    context_tokens: int = field(default_factory=lambda: int(os.getenv("MODEL_CONTEXT_TOKENS", "16384")))

    def blockers(self):
        result = []
        if len(self.token) < 32:
            result.append("HARNESS_API_TOKEN must contain at least 32 characters")
        if not self.model or not self.key:
            result.append("MODEL_NAME / MODEL_API_KEY not configured")
        if not self.authorized:
            result.append("Paid model calls not authorized (MODEL_CALLS_AUTHORIZED=false)")
        if not re.fullmatch(r"[\w./:-]+@sha256:[a-f0-9]{64}", self.image):
            result.append("HARNESS_IMAGE must be an operator-prepared image pinned by sha256 digest")
        if not shutil.which("docker"):
            result.append("Docker CLI unavailable; configure a Linux Docker worker")
        return result


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization[\s:=]+(?:bearer\s+)?)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_]{16,}\b", "[REDACTED]", text)
    return text
