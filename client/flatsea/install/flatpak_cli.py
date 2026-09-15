"""Subprocess wrapper around ``flatpak``, ``ostree`` and ``flatpak-builder``.

Everything is ``--user`` so no polkit prompts are needed. Steps are split so the
pipeline can pull first, look at the files, and only then deploy.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("flatsea.flatpak")

FLATPAK_USER_REPO = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "flatpak/repo"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "flatsea"
LOCAL_REMOTE = "flatsea-local"  # where locally built (manifest-only) apps are exported

# When Flatsea itself runs as a Flatpak, host commands go through the portal.
IN_SANDBOX = Path("/.flatpak-info").exists()
HOST_PREFIX = ["flatpak-spawn", "--host"] if IN_SANDBOX else []


class FlatpakError(RuntimeError):
    pass


def _run(cmd: list[str], on_line: Callable[[str], None] | None = None, check: bool = True,
         env: dict[str, str] | None = None) -> str:
    cmd = HOST_PREFIX + cmd
    log.debug("$ %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        env={**os.environ, "LC_ALL": "C.UTF-8", **(env or {})},
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if on_line and line.strip():
            on_line(line.strip())
    proc.wait()
    out = "\n".join(lines)
    if check and proc.returncode != 0:
        raise FlatpakError(f"{cmd[0]} failed ({proc.returncode}):\n{out[-2000:]}")
    return out


def available() -> bool:
    return shutil.which("flatpak") is not None


def default_arch() -> str:
    try:
        return _run(["flatpak", "--default-arch"]).strip() or "x86_64"
    except (FlatpakError, OSError):
        return "x86_64"


def installed_ids() -> set[str]:
    try:
        out = _run(["flatpak", "list", "--user", "--app", "--columns=application"])
    except (FlatpakError, OSError):
        return set()
    return {l.strip() for l in out.splitlines() if l.strip()}


def ensure_remote(name: str, url: str, gpg_verify: bool = True) -> None:
    cmd = ["flatpak", "remote-add", "--user", "--if-not-exists"]
    if not gpg_verify:
        cmd.append("--no-gpg-verify")
    _run(cmd + [name, url])


def pull(remote: str, ref: str, on_line=None) -> None:
    """Download into the local repo without deploying."""
    _run(["flatpak", "install", "--user", "--noninteractive", "--no-deploy", remote, ref], on_line)


def pull_bundle(path: Path, on_line=None) -> None:
    _run(["flatpak", "install", "--user", "--noninteractive", "--no-deploy", str(path)], on_line)


def deploy(remote: str, ref: str, on_line=None) -> None:
    """Second install: objects are already local, so this just deploys."""
    _run(["flatpak", "install", "--user", "--noninteractive", "--reinstall", remote, ref], on_line)


def deploy_bundle(path: Path, on_line=None) -> None:
    _run(["flatpak", "install", "--user", "--noninteractive", "--reinstall", str(path)], on_line)


def uninstall(app_id: str, on_line=None) -> None:
    _run(["flatpak", "uninstall", "--user", "--noninteractive", app_id], on_line)


def launch(app_id: str) -> None:
    subprocess.Popen(HOST_PREFIX + ["flatpak", "run", app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- looking at pulled-but-undeployed refs -------------------------------------


def local_refs(app_id: str, repo: Path = FLATPAK_USER_REPO) -> list[str]:
    """Refs in the ostree repo for this app, e.g. ``flathub:app/org.x.Y/x86_64/stable``."""
    if shutil.which("ostree") is None:
        return []
    try:
        out = _run(["ostree", f"--repo={repo}", "refs"])
    except FlatpakError:
        return []
    return [r for r in out.splitlines() if f"app/{app_id}/" in r]


def checkout(ref: str, dest: Path, repo: Path = FLATPAK_USER_REPO) -> Path:
    """Materialise a commit into ``dest`` so ClamAV can read it."""
    if shutil.which("ostree") is None:
        raise FlatpakError("ostree binary not found; install the 'ostree' package")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    commit = _run(["ostree", f"--repo={repo}", "rev-parse", ref]).strip()
    _run(["ostree", f"--repo={repo}", "checkout", "--user-mode", "--force-copy", commit, str(dest)])
    return dest


def read_metadata(checkout_dir: Path) -> str | None:
    p = checkout_dir / "metadata"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None


# --- building from a manifest -------------------------------------------------------


def build_from_manifest(manifest_path: Path, app_id: str, on_line=None) -> tuple[Path, str]:
    """Run flatpak-builder and export into a local repo. Returns (repo, ref)."""
    if shutil.which("flatpak-builder") is None:
        raise FlatpakError("flatpak-builder not found; install the 'flatpak-builder' package")
    repo = CACHE / "repo"
    build_dir = CACHE / "build" / app_id
    state_dir = CACHE / "builder-state"
    repo.mkdir(parents=True, exist_ok=True)
    _run(
        ["flatpak-builder", "--user", "--force-clean", "--install-deps-from=flathub", "--ccache",
         f"--state-dir={state_dir}", f"--repo={repo}", str(build_dir), str(manifest_path)],
        on_line,
    )
    out = _run(["ostree", f"--repo={repo}", "refs"])
    refs = [r for r in out.splitlines() if r.startswith(f"app/{app_id}/")]
    if not refs:
        raise FlatpakError("build finished but no app ref was exported")
    return repo, refs[0]


def deploy_local_build(repo: Path, ref: str, on_line=None) -> None:
    ensure_remote(LOCAL_REMOTE, str(repo), gpg_verify=False)
    _run(["flatpak", "install", "--user", "--noninteractive", "--reinstall", LOCAL_REMOTE, ref], on_line)
