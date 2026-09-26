"""Run ClamAV over a directory (or single file).

``clamscan`` spends most of a scan loading its signature database - 8-10 s even for a
3 MB app - so :class:`Scanner` starts it while the download is still running. It reads
the paths to scan from its stdin (``--file-list=/dev/stdin``), so it loads the
database, then waits for the pipeline to write the checked-out path. By the time the
download is done and unpacked the database is usually loaded, and the scan itself
takes a fraction of a second. If Flatsonar exits mid-download the pipe closes, and
clamscan sees an empty list and exits on its own instead of lingering.

A running ``clamd`` is faster still (the database stays loaded), so it is used when it
answers. ``clamdscan`` is often installed without the daemon running, so it is pinged
first; otherwise a missing daemon would read as a failed scan.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .flatpak_cli import HOST_PREFIX, host_which

log = logging.getLogger("flatsonar.scan")

TIMEOUT = 1800


@dataclass
class ScanResult:
    ran: bool  # False when clamscan is not installed, or there was nothing to scan
    infected: list[tuple[str, str]] = field(default_factory=list)  # (path, signature)
    scanned_files: int = 0
    error: str | None = None
    skipped: str | None = None  # why nothing was scanned although ClamAV may be there

    @property
    def clean(self) -> bool:
        return self.ran and not self.infected and self.error is None


def clamav_available() -> bool:
    return host_which("clamscan") is not None or host_which("clamdscan") is not None


def _clamd_answers(exe: str) -> bool:
    try:
        return subprocess.run(HOST_PREFIX + [exe, "--ping", "1"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _clamd_cmd() -> list[str] | None:
    exe = host_which("clamdscan")
    if exe and _clamd_answers(exe):
        return [exe, "--infected", "--no-summary", "--fdpass", "--multiscan"]
    return None


def _parse(returncode: int, stdout: str, stderr: str, path: Path) -> ScanResult:
    result = ScanResult(ran=True)
    # 0 = clean, 1 = virus found, 2 = error
    if returncode == 2:
        result.error = (stderr or stdout).strip()[-500:] or "clamscan error"
    for line in stdout.splitlines():
        if line.endswith(" FOUND"):
            file_part, _, sig = line[: -len(" FOUND")].rpartition(": ")
            result.infected.append((file_part, sig))
    result.scanned_files = sum(1 for _ in path.rglob("*")) if path.is_dir() else 1
    return result


def scan(path: Path) -> ScanResult:
    """One cold scan: pays for loading the database now. See :class:`Scanner`."""
    cmd = _clamd_cmd()
    if cmd is None:
        exe = host_which("clamscan")
        if exe is None:
            return ScanResult(ran=False)
        cmd = [exe, "--infected", "--no-summary", "--recursive"]
    cmd = HOST_PREFIX + cmd + [str(path)]
    log.info("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return ScanResult(ran=True, error="ClamAV timed out")
    except OSError as exc:
        return ScanResult(ran=True, error=str(exc))
    return _parse(proc.returncode, proc.stdout, proc.stderr, path)


class Scanner:
    """ClamAV started ahead of time. ``scan()`` at most once, then ``close()``."""

    def __init__(self) -> None:
        self._clamd: list[str] | None = None
        self._proc: subprocess.Popen | None = None
        self._closed = False
        self._lock = threading.Lock()
        # Picking the engine means host round trips (and a clamd ping); keep them off
        # the caller's path, which is about to start the download.
        self._ready = threading.Thread(target=self._start, name="clamav-warmup", daemon=True)
        self._ready.start()

    def _start(self) -> None:
        clamd = _clamd_cmd()
        with self._lock:
            if self._closed:
                return
            if clamd is not None:
                self._clamd = clamd
                return
            exe = host_which("clamscan")
            if exe is None:
                return
            cmd = HOST_PREFIX + [exe, "--infected", "--no-summary", "--recursive", "--file-list=/dev/stdin"]
            log.info("$ %s  (warming up)", " ".join(cmd))
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True, errors="replace")
            except OSError as exc:
                log.warning("could not start clamscan: %s", exc)

    def scan(self, path: Path) -> ScanResult:
        self._ready.join()
        if self._clamd is not None:
            cmd = HOST_PREFIX + self._clamd + [str(path)]
            log.info("$ %s", " ".join(cmd))
            try:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      errors="replace", timeout=TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                return ScanResult(ran=True, error="ClamAV timed out")
            except OSError as exc:
                return ScanResult(ran=True, error=str(exc))
            return _parse(proc.returncode, proc.stdout, proc.stderr, path)
        if self._proc is None:
            return ScanResult(ran=False)
        try:
            out, err = self._proc.communicate(input=f"{path}\n", timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.close()
            return ScanResult(ran=True, error="ClamAV timed out")
        return _parse(self._proc.returncode, out, err, path)

    def close(self) -> None:
        """Nothing (more) to scan: stop a clamscan that is still loading or waiting."""
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        # SIGTERM reaches the host process too: flatpak-spawn forwards it.
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
