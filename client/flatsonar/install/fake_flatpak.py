"""A stand-in for ``flatpak`` so the whole client can be driven on a machine without
one (Windows under MSYS2, a CI box). ``FLATSONAR_FAKE_FLATPAK=1`` swaps every function
in :mod:`flatpak_cli` for an in-memory simulation; ``=reset`` starts from the seeded
scenario again.

Nothing is downloaded, built or run. Installs and updates stage a commit ("pulled"),
hand the pipeline a real ``metadata`` file to check out and score, then "deploy" it.
The scenario is picked so every path in the Installed page shows up at once: an update
that changes nothing, an update that grows the sandbox, a red app, a local build the
index has a newer version of, and an app the index has never heard of. State persists
in ``<cache>/flatsonar/fake-flatpak.json``; edit it to make your own scenario.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from flatsonar_core import parse_manifest
from flatsonar_core.manifest import ManifestError

from . import flatpak_cli as fp
from .flatpak_cli import InstalledApp

log = logging.getLogger("flatsonar.fake-flatpak")

STATE_FILE = fp.CACHE / "fake-flatpak.json"
PACE = 0.25  # seconds per simulated progress line; keeps the status bar readable


# --- metadata -------------------------------------------------------------------------


def metadata_from_finish_args(app_id: str, finish_args: list[str]) -> str:
    """The inverse of :func:`flatsonar_core.metadata_to_finish_args`, good enough for
    what the risk scorer reads."""
    ctx: dict[str, list[str]] = {"shared": [], "sockets": [], "devices": [], "filesystems": [], "features": []}
    keys = {"share": "shared", "socket": "sockets", "device": "devices", "filesystem": "filesystems",
            "allow": "features"}
    session: dict[str, str] = {}
    system: dict[str, str] = {}
    env: dict[str, str] = {}
    persistent: list[str] = []
    for arg in finish_args:
        if not arg.startswith("--"):
            continue
        key, _, val = arg[2:].partition("=")
        if key in keys:
            ctx[keys[key]].append(val)
        elif key == "talk-name":
            session[val] = "talk"
        elif key == "own-name":
            session[val] = "own"
        elif key == "system-talk-name":
            system[val] = "talk"
        elif key == "system-own-name":
            system[val] = "own"
        elif key == "env":
            k, _, v = val.partition("=")
            env[k] = v
        elif key == "persist":
            persistent.append(val)
    lines = ["[Application]", f"name={app_id}", "runtime=org.gnome.Platform/x86_64/48",
             "sdk=org.gnome.Sdk/x86_64/48", "", "[Context]"]
    for k, vs in ctx.items():
        if vs:
            lines.append(f"{k}={';'.join(vs)};")
    if persistent:
        lines.append(f"persistent={';'.join(persistent)};")
    for title, table in (("Session Bus Policy", session), ("System Bus Policy", system), ("Environment", env)):
        if table:
            lines += ["", f"[{title}]"] + [f"{k}={v}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


# --- state -------------------------------------------------------------------------------


@dataclass
class FakeApp:
    app_id: str
    version: str
    origin: str  # flathub | flatsonar-local | "" (bundle) | a remote name
    installation: str = "user"
    name: str = ""
    finish_args: list[str] = field(default_factory=list)
    # What the remote would hand over on update: (version, finish-args). None = up to date.
    next_version: str | None = None
    next_finish_args: list[str] | None = None

    def metadata(self) -> str:
        return metadata_from_finish_args(self.app_id, self.finish_args)


@dataclass
class Staged:
    """Pulled or built but not deployed: what ``checkout`` materialises."""

    app_id: str
    version: str
    origin: str
    installation: str = "user"
    finish_args: list[str] | None = None  # None: nothing known, let the index's permissions stand


_SANDBOXED = ["--share=ipc", "--share=network", "--socket=wayland", "--socket=fallback-x11", "--device=dri"]

SCENARIO: list[FakeApp] = [
    # Update that changes nothing: no dialog, just "updated".
    FakeApp("ai.jan.Jan", "0.8.3", "flathub", "system", "Jan", _SANDBOXED,
            next_version="0.8.4", next_finish_args=_SANDBOXED),
    # Update that grows the sandbox: gate two warns twice about --filesystem=host.
    FakeApp("app.cantara.Cantara", "2.7.0", "flathub", "user", "Cantara", [*_SANDBOXED, "--socket=x11"],
            next_version="2.7.1", next_finish_args=[*_SANDBOXED, "--socket=x11", "--filesystem=host"]),
    # Installed and red, nothing new; the audit value of the page.
    FakeApp("app.devsuite.Ptyxis", "50.1", "flathub", "user", "Ptyxis",
            [*_SANDBOXED, "--talk-name=org.freedesktop.Flatpak", "--filesystem=host"]),
    # Built locally by Flatsonar; the index lists 0.5.1, so Update rebuilds from the manifest.
    FakeApp("garden.turtle.Jellybean", "0.5.0", fp.LOCAL_REMOTE, "user", "Stockpile", _SANDBOXED,
            next_version="0.5.1"),
    # Not in the index at all, from a bundle, and dangerous.
    FakeApp("com.example.Legacy", "3.2", "", "user", "Legacy Tool",
            [*_SANDBOXED, "--device=all", "--filesystem=home"]),
]


class FakeFlatpak:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.apps: dict[str, FakeApp] = {}
        self.staged: dict[str, Staged] = {}

    # -- persistence --------------------------------------------------------------------

    def seed(self) -> None:
        self.apps = {a.app_id: FakeApp(**asdict(a)) for a in SCENARIO}
        self.staged = {}
        self.save()

    def load(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.apps = {a["app_id"]: FakeApp(**a) for a in data["apps"]}
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"apps": [asdict(a) for a in self.apps.values()]}, indent=1),
                             encoding="utf-8")

    # -- helpers ------------------------------------------------------------------------

    @staticmethod
    def _app_id(ref: str) -> str:
        parts = ref.split(":", 1)[-1].split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "app" else ref

    @staticmethod
    def _progress(on_line, what: str, steps: int = 4) -> None:
        for i in range(1, steps + 1):
            time.sleep(PACE)
            if on_line:
                on_line(f"[fake flatpak] {what} {i * 100 // steps}%")

    @staticmethod
    def _transaction(on_line, app_id: str, steps: int = 4, verb: str = "Installing") -> None:
        """What ``flatpak install -y`` prints without a terminal (see install/progress.py)."""
        if on_line:
            on_line(f" 1.\t   \t{app_id}\tstable\ti\tflathub\t< 12.4\xa0MB")
        for i in range(steps + 1):
            time.sleep(PACE)
            pct = i * 100 // steps
            if on_line:
                on_line(f"{verb} 1/1… {'█' * (pct // 10):<10}  {pct:>3}%  3.1\xa0MB/s  00:0{steps - i}")

    # -- flatpak_cli surface --------------------------------------------------------------

    def available(self) -> bool:
        return True

    def default_arch(self) -> str:
        return "x86_64"

    def installed_apps(self) -> list[InstalledApp]:
        return [InstalledApp(a.app_id, a.version, a.origin, a.installation,
                             f"app/{a.app_id}/x86_64/stable", a.name) for a in self.apps.values()]

    def installed_ids(self) -> set[str]:
        return set(self.apps)

    def deployed_metadata(self, app_id: str, installation: str = "user") -> str | None:
        a = self.apps.get(app_id)
        return a.metadata() if a and a.finish_args else None

    def updates_available(self) -> set[str]:
        time.sleep(PACE * 2)  # remote-ls talks to the network
        return {a.app_id for a in self.apps.values()
                if a.next_version and a.origin and a.origin != fp.LOCAL_REMOTE}

    def ensure_remote(self, name: str, url: str, gpg_verify: bool = True) -> None:
        log.info("[fake flatpak] remote-add %s %s", name, url)

    def pull(self, remote: str, ref: str, on_line=None) -> None:
        app_id = self._app_id(ref)
        known = self.apps.get(app_id)
        self._transaction(on_line, app_id)
        self.staged[app_id] = Staged(app_id, known.version if known else "1.0", remote,
                                     finish_args=list(known.finish_args) if known else None)

    def pull_bundle(self, path: Path, on_line=None) -> None:
        app_id = Path(path).name.removesuffix(".flatpak")
        self._transaction(on_line, app_id)
        self.staged[app_id] = Staged(app_id, "1.0", "")

    def pull_update(self, app_id: str, installation: str = "user", on_line=None) -> None:
        a = self.apps.get(app_id)
        if a is None:
            raise fp.FlatpakError(f"[fake flatpak] {app_id} is not installed")
        self._transaction(on_line, app_id, verb="Updating")
        if a.next_version:
            self.staged[app_id] = Staged(app_id, a.next_version, a.origin, installation,
                                         list(a.next_finish_args if a.next_finish_args is not None else a.finish_args))
        else:
            self.staged[app_id] = Staged(app_id, a.version, a.origin, installation, list(a.finish_args))

    def local_refs(self, app_id: str, repo: Path | None = None) -> list[str]:
        s = self.staged.get(app_id)
        if s is not None and s.finish_args is not None:
            return [f"{s.origin or 'bundle'}:app/{app_id}/x86_64/stable"]
        return []

    def checkout(self, ref: str, dest: Path, repo: Path | None = None) -> Path:
        app_id = self._app_id(ref)
        s = self.staged.get(app_id)
        if s is None or s.finish_args is None:
            raise fp.FlatpakError(f"[fake flatpak] nothing pulled for {app_id}")
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "files" / "bin").mkdir(parents=True)
        (dest / "files" / "bin" / app_id).write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        (dest / "metadata").write_text(metadata_from_finish_args(app_id, s.finish_args), encoding="utf-8")
        return dest

    def _deploy_staged(self, app_id: str, origin: str | None = None, installation: str | None = None) -> None:
        s = self.staged.pop(app_id, None)
        if s is None:
            raise fp.FlatpakError(f"[fake flatpak] nothing to deploy for {app_id}")
        old = self.apps.get(app_id)
        self.apps[app_id] = FakeApp(
            app_id, s.version, origin if origin is not None else s.origin,
            installation or s.installation, old.name if old else app_id.rsplit(".", 1)[-1],
            s.finish_args if s.finish_args is not None else (old.finish_args if old else []),
        )
        self.save()

    def deploy(self, remote: str, ref: str, on_line=None) -> None:
        app_id = self._app_id(ref)
        self._transaction(on_line, app_id, 2)
        self._deploy_staged(app_id, origin=remote)

    def deploy_bundle(self, path: Path, on_line=None) -> None:
        app_id = Path(path).name.removesuffix(".flatpak")
        self._transaction(on_line, app_id, 2)
        self._deploy_staged(app_id, origin="")

    def deploy_update(self, app_id: str, installation: str = "user", on_line=None) -> None:
        self._transaction(on_line, app_id, 2, verb="Updating")
        self._deploy_staged(app_id, installation=installation)

    def uninstall(self, app_id: str, on_line=None, installation: str = "user") -> None:
        self._progress(on_line, f"removing {app_id}", 2)
        if self.apps.pop(app_id, None) is None:
            raise fp.FlatpakError(f"[fake flatpak] {app_id} is not installed")
        self.save()

    def launch(self, app_id: str) -> None:
        raise OSError(f"[fake flatpak] would run {app_id}")

    def build_from_manifest(self, manifest_path: Path, app_id: str, on_line=None) -> tuple[Path, str]:
        try:
            manifest = parse_manifest(Path(manifest_path))
            finish_args = list(manifest.finish_args)
        except (ManifestError, OSError) as exc:
            raise fp.FlatpakError(f"[fake flatpak] cannot build: {exc}") from exc
        for name in ("runtime", "deps", app_id.rsplit(".", 1)[-1].lower()):
            self._progress(on_line, f"building module {name}", 3)
        old = self.apps.get(app_id)
        version = (old.next_version if old and old.next_version else None) or (old.version if old else "1.0")
        self.staged[app_id] = Staged(app_id, version, fp.LOCAL_REMOTE, finish_args=finish_args)
        repo = fp.CACHE / "fake-repo"
        repo.mkdir(parents=True, exist_ok=True)
        return repo, f"app/{app_id}/x86_64/master"

    def deploy_local_build(self, repo: Path, ref: str, on_line=None) -> None:
        app_id = self._app_id(ref)
        self._transaction(on_line, app_id, 2)
        self._deploy_staged(app_id, origin=fp.LOCAL_REMOTE)


_PATCHED = (
    "available", "default_arch", "installed_apps", "installed_ids", "deployed_metadata", "updates_available",
    "ensure_remote", "pull", "pull_bundle", "pull_update", "local_refs", "checkout", "deploy", "deploy_bundle",
    "deploy_update", "uninstall", "launch", "build_from_manifest", "deploy_local_build",
)


def activate(reset: bool = False, path: Path = STATE_FILE) -> FakeFlatpak:
    """Replace :mod:`flatpak_cli`'s functions. Callers that did ``from . import
    flatpak_cli as fp`` pick the fakes up automatically."""
    fake = FakeFlatpak(path)
    if reset or not fake.load():
        fake.seed()
    for name in _PATCHED:
        setattr(fp, name, getattr(fake, name))
    log.warning("[fake flatpak] active: %d simulated apps, state in %s", len(fake.apps), path)
    return fake
