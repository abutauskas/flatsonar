"""Run ClamAV over a directory (or single file)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("flatsonar.scan")


@dataclass
class ScanResult:
    ran: bool  # False when clamscan is not installed
    infected: list[tuple[str, str]] = field(default_factory=list)  # (path, signature)
    scanned_files: int = 0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return self.ran and not self.infected and self.error is None


def clamav_available() -> bool:
    return shutil.which("clamscan") is not None or shutil.which("clamdscan") is not None


def scan(path: Path) -> ScanResult:
    exe = shutil.which("clamdscan") or shutil.which("clamscan")
    if exe is None:
        return ScanResult(ran=False)
    cmd = [exe, "--infected", "--no-summary"]
    if exe.endswith("clamscan"):
        cmd += ["--recursive"]
    else:
        cmd += ["--fdpass", "--multiscan"]
    cmd.append(str(path))
    log.info("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return ScanResult(ran=True, error="ClamAV timed out")
    except OSError as exc:
        return ScanResult(ran=True, error=str(exc))

    result = ScanResult(ran=True)
    # 0 = clean, 1 = virus found, 2 = error
    if proc.returncode == 2:
        result.error = (proc.stderr or proc.stdout).strip()[-500:] or "clamscan error"
    for line in proc.stdout.splitlines():
        if line.endswith(" FOUND"):
            file_part, _, sig = line[: -len(" FOUND")].rpartition(": ")
            result.infected.append((file_part, sig))
    result.scanned_files = sum(1 for _ in path.rglob("*")) if path.is_dir() else 1
    return result
