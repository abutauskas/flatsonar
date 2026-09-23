"""Subprocess wrapper around ``flatpak``, ``ostree`` and ``flatpak-builder``.

Everything is ``--user`` so no polkit prompts are needed. Steps are split so the
pipeline can pull first, look at the files, and only then deploy.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..paths import cache_dir, data_dir

log = logging.getLogger("flatsonar.flatpak")

FLATPAK_USER_ROOT = data_dir() / "flatpak"
FLATPAK_SYSTEM_ROOT = Path("/var/lib/flatpak")
FLATPAK_USER_REPO = FLATPAK_USER_ROOT / "repo"
CACHE = cache_dir() / "flatsonar"
LOCAL_REMOTE = "flatsonar-local"  # where locally built (manifest-only) apps are exported
INSTALLATIONS = ("user", "system")

# When Flatsonar itself runs as a Flatpak, host commands go through the portal.
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


def host_which(name: str) -> str | None:
    """Resolve a command on the *host*. ``shutil.which`` only ever sees this sandbox's
    own filesystem, which never ships host tooling (flatpak, ostree, flatpak-builder,
    clamscan, ...) - checking it always says "not found" regardless of the host."""
    if not IN_SANDBOX:
        return shutil.which(name)
    try:
        out = _run(["sh", "-c", f"command -v {shlex.quote(name)}"], check=False).strip()
    except OSError:
        return None
    return out or None


def available() -> bool:
    return host_which("flatpak") is not None


def default_arch() -> str:
    try:
        return _run(["flatpak", "--default-arch"]).strip() or "x86_64"
    except (FlatpakError, OSError):
        return "x86_64"


@dataclass
class InstalledApp:
    """One row of ``flatpak list``. ``installation`` is ``user`` or ``system`` (or a named
    system installation); ``origin`` is the remote it came from, ``flatsonar-local`` for
    apps Flatsonar built from a manifest, and empty for bundles."""

    app_id: str
    version: str = ""
    origin: str = ""
    installation: str = "user"
    ref: str = ""
    name: str = ""

    @property
    def locally_built(self) -> bool:
        return self.origin == LOCAL_REMOTE


_LIST_COLUMNS = "application,version,origin,installation,ref,name"


def parse_installed(text: str) -> list[InstalledApp]:
    """``flatpak list --columns=...`` prints tab-separated rows when not on a terminal."""
    out: list[InstalledApp] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        cols += [""] * (6 - len(cols))
        app_id, version, origin, installation, ref, name = (c.strip() for c in cols[:6])
        if not app_id:
            continue
        out.append(InstalledApp(app_id, version, origin, installation or "user", ref, name))
    return out


def installed_apps() -> list[InstalledApp]:
    """Every installed app, user *and* system. Distros install Flathub apps system-wide by
    default, so listing only ``--user`` would offer to install things already there."""
    try:
        out = _run(["flatpak", "list", "--app", f"--columns={_LIST_COLUMNS}"])
    except (FlatpakError, OSError):
        return []
    return parse_installed(out)


def installed_ids() -> set[str]:
    return {a.app_id for a in installed_apps()}


def installed_runtimes() -> list[InstalledApp]:
    """Every installed runtime (org.gnome.Platform, org.kde.Platform, ...), user *and*
    system. Same row shape as :func:`installed_apps`; ``app_id`` is the runtime id."""
    try:
        out = _run(["flatpak", "list", "--runtime", f"--columns={_LIST_COLUMNS}"])
    except (FlatpakError, OSError):
        return []
    return parse_installed(out)


def _installation_flag(installation: str) -> str:
    return f"--{installation}" if installation in INSTALLATIONS else f"--installation={installation}"


def repo_for(installation: str) -> Path:
    return FLATPAK_USER_REPO if installation == "user" else FLATPAK_SYSTEM_ROOT / "repo"


def deployed_metadata(app_id: str, installation: str = "user") -> str | None:
    """The ``metadata`` keyfile of the *deployed* app: what its sandbox allows right now."""
    root = FLATPAK_USER_ROOT if installation == "user" else FLATPAK_SYSTEM_ROOT
    p = root / "app" / app_id / "current" / "active" / "metadata"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:  # inside our own sandbox the host paths are not mounted; ask flatpak instead
        return _run(["flatpak", "info", "--show-metadata", _installation_flag(installation), app_id]) or None
    except (FlatpakError, OSError):
        return None


def _updates_for(installation: str) -> set[str]:
    try:
        out = _run(["flatpak", "remote-ls", "--updates", "--app", f"--{installation}", "--columns=application"])
    except (FlatpakError, OSError):
        return set()
    return {l.strip() for l in out.splitlines() if l.strip()}


def updates_available() -> set[str]:
    """App ids whose remote has a newer commit than what is deployed. Talks to the network -
    one flatpak-spawn round trip per installation - so running them concurrently instead of
    one after another roughly halves the wait. A failure (offline, no polkit for system
    remotes) just means "none known" for that one installation."""
    with ThreadPoolExecutor(max_workers=len(INSTALLATIONS)) as pool:
        results = pool.map(_updates_for, INSTALLATIONS)
    return set().union(*results)


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


def uninstall(app_id: str, on_line=None, installation: str = "user") -> None:
    _run(["flatpak", "uninstall", _installation_flag(installation), "--noninteractive", app_id], on_line)


def pull_update(app_id: str, installation: str = "user", on_line=None) -> None:
    """Fetch the new commit into the local repo without deploying it, so it can be
    checked out, scanned and re-scored first (same trick as :func:`pull`)."""
    _run(["flatpak", "update", _installation_flag(installation), "--noninteractive", "--no-deploy", app_id], on_line)


def deploy_update(app_id: str, installation: str = "user", on_line=None) -> None:
    _run(["flatpak", "update", _installation_flag(installation), "--noninteractive", app_id], on_line)


def launch(app_id: str) -> None:
    subprocess.Popen(HOST_PREFIX + ["flatpak", "run", app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- looking at pulled-but-undeployed refs -------------------------------------


def local_refs(app_id: str, repo: Path = FLATPAK_USER_REPO) -> list[str]:
    """Refs in the ostree repo for this app, e.g. ``flathub:app/org.x.Y/x86_64/stable``."""
    if host_which("ostree") is None:
        return []
    try:
        out = _run(["ostree", f"--repo={repo}", "refs"])
    except FlatpakError:
        return []
    return [r for r in out.splitlines() if f"app/{app_id}/" in r]


def checkout(ref: str, dest: Path, repo: Path = FLATPAK_USER_REPO) -> Path:
    """Materialise a commit into ``dest`` so ClamAV can read it."""
    if host_which("ostree") is None:
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
    if host_which("flatpak-builder") is None:
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
