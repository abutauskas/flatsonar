"""pull -> look at the files -> scan -> re-score -> warn (twice) -> deploy.

Runs on a worker thread. UI interaction goes through the ``Confirmer`` callback
object, which the window implements with libadwaita dialogs.

Two gates, because two different things can be wrong:

1. **Before anything runs.** Who published this, and what does the manifest *do*?
   The index's trust findings (publisher namespace, id collisions, repo age) plus,
   for manifest installs, a fresh audit of the manifest we just downloaded (never
   the index's copy). For a manifest install this gate comes *before*
   flatpak-builder starts: the build is the risk.
2. **After pull / build.** The sandbox permissions from the real ``metadata`` file
   and the ClamAV result. Only shown again if it adds findings the first gate did
   not already list.

Either gate at yellow or above means two "Sure" dialogs. Accepted findings are
remembered per app until they change.

Updates go through gate two again (:func:`update`): the new commit is pulled with
``--no-deploy``, checked out, scanned and re-scored, and the user is only asked
when the update *adds* something they have not already accepted.

``status`` callbacks get ``(text, fraction)``: fraction is 0..1 while flatpak reports
download progress, None for steps that cannot say how far along they are.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from flatsonar_core import (
    RiskLevel,
    RiskReport,
    audit_manifest,
    describe_template_problems,
    manifest_template_problems,
    metadata_to_finish_args,
    parse_manifest,
    score_finish_args,
)
from flatsonar_core.manifest import ManifestError

from ..api import AppInfo, FlatsonarAPI, InstallSource
from ..state import Decisions
from . import flatpak_cli as fp
from .flatpak_cli import InstalledApp
from .progress import FlatpakProgress
from .scan import Scanner, ScanResult, clamav_available, scan

log = logging.getLogger("flatsonar.install")

# Anything at or above this level gets the two-step "Sure" flow.
DOUBLE_CONFIRM_FROM = RiskLevel.YELLOW

Status = Callable[[str, "float | None"], None]


def start_scanner() -> Scanner | None:
    """ClamAV, warming up while the download runs (see :class:`scan.Scanner`)."""
    return Scanner()


class Confirmer(Protocol):
    def warn_and_confirm(self, app: AppInfo, report: RiskReport, scan_result: ScanResult | None) -> bool:
        """First dialog: what is wrong. ``scan_result`` is None before anything was fetched.
        Returns True if the user pressed Sure."""

    def confirm_again(self, app: AppInfo) -> bool:
        """Second dialog: it's on you. Returns True if the user pressed Sure again."""

    def confirm_plain(self, app: AppInfo, report: RiskReport) -> bool:
        """Green app: a normal install confirmation."""


@dataclass
class Outcome:
    installed: bool
    cancelled: bool = False
    report: RiskReport | None = None
    scan: ScanResult | None = None
    message: str = ""


def pick_source(sources: list[InstallSource]) -> InstallSource | None:
    order = {"flathub": 0, "remote": 1, "bundle": 2, "manifest": 3}
    return min(sources, key=lambda s: order.get(s.kind, 9), default=None)


# --- versions ----------------------------------------------------------------------


def _numeric(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v))


def version_newer(latest: str | None, installed: str | None) -> bool:
    """Is the index's ``latest_version`` newer than what is deployed? Unknown -> False.
    Only apps a remote cannot update (local builds, bundles) rely on this."""
    if not latest or not installed or latest.strip() == installed.strip():
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest.strip().lstrip("vV")) > Version(installed.strip().lstrip("vV"))
        except InvalidVersion:
            pass
    except ImportError:
        pass
    return _numeric(latest) > _numeric(installed)


def _newest_installed_runtime_branch(runtime_id: str) -> str | None:
    """The highest numeric branch of ``runtime_id`` already installed (user or system),
    e.g. "48" if GNOME 46/47/48 are all installed. None if none is installed, or none
    of the installed branches are plain integers (a "master"/name branch, say)."""
    branches = [int(b) for b in (r.ref.rsplit("/", 1)[-1] for r in fp.installed_runtimes() if r.app_id == runtime_id)
                if b.isdigit()]
    return str(max(branches)) if branches else None


def update_available(app: AppInfo, installed: InstalledApp, remote_updates: set[str]) -> bool:
    """Remote-installed apps: whatever ``flatpak remote-ls --updates`` said. Local builds
    and bundles have no remote, so the index's latest version is the only signal."""
    if installed.locally_built or not installed.origin:
        return version_newer(app.latest_version, installed.version)
    return app.app_id in remote_updates


# --- trust ------------------------------------------------------------------------


def trust_report(app: AppInfo, source: InstallSource | None = None) -> RiskReport:
    """What the index knows about the publisher, as findings the dialogs can list."""
    report = RiskReport(RiskLevel.GREEN)
    for f in app.trust_findings:
        try:
            level = RiskLevel.from_label(f.get("level", "green"))
        except KeyError:
            level = RiskLevel.YELLOW
        report.escalate(level, f.get("check", "publisher"), f.get("reason", ""))
    # Older servers (or hand-made AppInfo) may send a level without findings.
    who = app.developer_name or "the publisher"
    if app.trust == "suspicious" and report.level < RiskLevel.RED:
        report.escalate(RiskLevel.RED, "publisher", f"Flatsonar found a red flag about who publishes {app.app_id}")
    elif app.trust == "unverified" and report.level < RiskLevel.YELLOW:
        report.escalate(RiskLevel.YELLOW, "publisher", f"nobody has verified that {who} controls {app.app_id}")
    if source is not None and source.kind == "remote" and app.trust != "verified" and source.remote_url:
        host = urlsplit(source.remote_url).hostname or source.remote_url
        report.escalate(RiskLevel.YELLOW, "remote",
                        f"adds the third-party Flatpak remote {source.remote_name or host}: "
                        "future updates of this app will come from it, not from Flatsonar")
    return report


# --- prepare ------------------------------------------------------------------------


class _Prepared:
    """A pulled-but-not-deployed app plus where its files were checked out."""

    def __init__(self, source: InstallSource, installation: str = "user", update: bool = False):
        self.source = source
        self.installation = installation
        self.update = update  # deploy with ``flatpak update`` instead of ``install``
        self.checkout: Path | None = None
        self.bundle_path: Path | None = None
        self.local_repo: Path | None = None
        self.ref: str | None = None
        self.scanner: Scanner | None = None


def _status(cb: Status | None, msg: str, fraction: float | None = None) -> None:
    log.info(msg)
    if cb:
        cb(msg, fraction)


def _flatpak_progress(status: Status | None, app: AppInfo, verb: str) -> Callable[[str], None]:
    """An ``on_line`` for flatpak transactions: progress ticks become status updates,
    the rest of flatpak's chatter (tables, permission lists) is only logged."""
    parser = FlatpakProgress(app.app_id, app.name, verb)

    def on_line(line: str) -> None:
        tick = parser.feed(line)
        if tick is None:
            log.debug("flatpak: %s", line)
        elif status:
            status(*tick)

    return on_line


def _fetch_manifest(app: AppInfo, source: InstallSource, api: FlatsonarAPI, status) -> Path:
    assert source.manifest_url
    mdir = fp.CACHE / "manifests" / app.app_id
    mdir.mkdir(parents=True, exist_ok=True)
    mpath = mdir / source.manifest_url.rsplit("/", 1)[-1]
    _status(status, "Fetching manifest…")
    api.download(source.manifest_url, str(mpath))
    return mpath


def audit_downloaded_manifest(app: AppInfo, mpath: Path) -> RiskReport:
    """Audit the manifest we are about to build, not the copy the index saw."""
    report = RiskReport(RiskLevel.GREEN)
    try:
        manifest = parse_manifest(mpath)
    except (ManifestError, OSError) as exc:
        return report.escalate(RiskLevel.RED, "manifest", f"could not read the manifest: {exc}")
    if manifest.app_id != app.app_id and not manifest.app_id.startswith(app.app_id + "."):
        report.escalate(RiskLevel.RED, "manifest:app-id",
                        f"the manifest builds {manifest.app_id}, not {app.app_id}")
    report.extend(audit_manifest(manifest))
    return report


def template_problems(mpath: Path) -> list[str]:
    """Why the downloaded manifest can't be built as committed (an unfilled release
    template such as VERSION_PLACEHOLDER in its URLs); [] when it can, or when it does
    not parse at all - :func:`audit_downloaded_manifest` reports that one."""
    try:
        return manifest_template_problems(parse_manifest(mpath))
    except (ManifestError, OSError):
        return []


def _checkout_local(app: AppInfo, prep: _Prepared, origin: str | None, repo: Path | None, status) -> None:
    """Materialise the pulled ref for inspection, preferring the ref from ``origin``."""
    kwargs = {"repo": repo} if repo is not None else {}
    refs = fp.local_refs(app.app_id, **kwargs)
    local = [r for r in refs if origin and r.startswith(f"{origin}:")] or refs
    if local:
        _status(status, "Unpacking for inspection…")
        prep.checkout = fp.checkout(local[0], fp.CACHE / "scan" / app.app_id, **kwargs)


def _pull(app: AppInfo, source: InstallSource, api: FlatsonarAPI, status) -> _Prepared:
    """flathub / remote / bundle: download without deploying, check out for inspection."""
    prep = _Prepared(source)

    if source.kind in ("flathub", "remote"):
        remote = source.remote_name or "flathub"
        if source.remote_url:
            _status(status, f"Adding remote {remote}…")
            fp.ensure_remote(remote, source.remote_url)
        ref = source.ref or f"app/{app.app_id}/{fp.default_arch()}/stable"
        _status(status, f"Downloading {app.name} (not installing yet)…", 0.0)
        fp.pull(remote, ref, on_line=_flatpak_progress(status, app, "Downloading"))
        prep.ref = ref
        _checkout_local(app, prep, remote, None, status)

    elif source.kind == "bundle" and source.bundle_url:
        dest = fp.CACHE / "bundles" / f"{app.app_id}.flatpak"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _status(status, f"Downloading {app.name} bundle…", 0.0)
        api.download(source.bundle_url, str(dest),
                     progress=lambda f: status and status(f"Downloading {app.name} bundle · {f:.0%}", f))
        prep.bundle_path = dest
        _status(status, "Importing bundle (not installing yet)…")
        fp.pull_bundle(dest, on_line=_flatpak_progress(status, app, "Importing"))
        _checkout_local(app, prep, None, None, status)
    else:
        raise fp.FlatpakError(f"no usable install source for {app.app_id}")
    return prep


def _pull_update(app: AppInfo, installed: InstalledApp, status) -> _Prepared:
    """``flatpak update --no-deploy`` from wherever the app was installed, then check out
    the new commit so gate two can look at it."""
    prep = _Prepared(InstallSource(kind="update"), installation=installed.installation, update=True)
    _status(status, f"Downloading {app.name} update (not installing yet)…", 0.0)
    fp.pull_update(app.app_id, installed.installation, on_line=_flatpak_progress(status, app, "Downloading"))
    prep.ref = installed.ref or None
    _checkout_local(app, prep, installed.origin or None, fp.repo_for(installed.installation), status)
    return prep


def _prefer_installed_gnome_runtime(mpath: Path, status) -> Path:
    """GNOME runtimes are meant to be forward-compatible, and flatpak-builder happily
    builds against a newer org.gnome.Platform branch than a manifest asks for. Rather
    than pull a second, separate runtime just for this one app, reuse whichever GNOME
    branch is already the newest on this machine - unless that would mean *downgrading*
    below what the manifest declares, which is far more likely to actually break the
    build than a newer one is."""
    try:
        manifest = parse_manifest(mpath)
    except (ManifestError, OSError):
        return mpath
    if manifest.runtime != "org.gnome.Platform":
        return mpath
    newest = _newest_installed_runtime_branch(manifest.runtime)
    declared = manifest.runtime_version
    if not newest or (declared and declared.isdigit() and int(newest) <= int(declared)):
        return mpath

    _status(status, f"Using your installed GNOME {newest} runtime instead of {declared or 'downloading a new one'}…")
    raw = dict(manifest.raw)
    raw["runtime-version"] = newest
    if "sdk-version" in raw:  # rare, but keep runtime and sdk in lockstep if it's set
        raw["sdk-version"] = newest
    pinned = mpath.with_name(f"{mpath.stem}.gnome{newest}.json")
    pinned.write_text(json.dumps(raw), encoding="utf-8")
    return pinned


def _build(app: AppInfo, source: InstallSource, mpath: Path, status) -> _Prepared:
    prep = _Prepared(source)
    mpath = _prefer_installed_gnome_runtime(mpath, status)
    _status(status, f"Building {app.name} from source with flatpak-builder (this can take a while)…")
    repo, ref = fp.build_from_manifest(mpath, app.app_id, on_line=status)
    prep.local_repo, prep.ref = repo, ref
    _status(status, "Unpacking for inspection…")
    prep.checkout = fp.checkout(ref, fp.CACHE / "scan" / app.app_id, repo=repo)
    return prep


def _score(app: AppInfo, prep: _Prepared) -> RiskReport:
    """Re-score from the real metadata when we have it; otherwise trust the index."""
    if prep.checkout:
        meta = fp.read_metadata(prep.checkout)
        if meta:
            return score_finish_args(metadata_to_finish_args(meta), app.app_id)
    return score_finish_args([p["arg"] for p in app.permissions], app.app_id)


def _deploy(app: AppInfo, prep: _Prepared, status) -> None:
    src = prep.source
    verb = "Updating" if prep.update else "Installing"
    _status(status, f"{verb} {app.name}…")
    on_line = _flatpak_progress(status, app, verb)
    if prep.update:
        fp.deploy_update(app.app_id, prep.installation, on_line=on_line)
    elif src.kind in ("flathub", "remote"):
        fp.deploy(src.remote_name or "flathub", prep.ref or "", on_line=on_line)
    elif src.kind == "bundle":
        assert prep.bundle_path
        fp.deploy_bundle(prep.bundle_path, on_line=on_line)
    elif src.kind == "manifest":
        assert prep.local_repo and prep.ref
        fp.deploy_local_build(prep.local_repo, prep.ref, on_line=on_line)


def _scan(prep: _Prepared, status) -> ScanResult:
    target = prep.checkout or prep.bundle_path
    if target is None:
        if clamav_available():
            return ScanResult(ran=False, skipped="ostree is not installed, so the download could not be "
                                                 "unpacked for ClamAV")
        return ScanResult(ran=False)
    _status(status, "Scanning with ClamAV…")
    return prep.scanner.scan(target) if prep.scanner is not None else scan(target)


# --- the flow -------------------------------------------------------------------------


def _gate(app: AppInfo, report: RiskReport, scan_result: ScanResult | None,
          confirmer: Confirmer, decisions: Decisions) -> bool:
    """Warn twice if needed. Returns False when the user backed out."""
    if report.level < DOUBLE_CONFIRM_FROM:
        return True
    if decisions.already_accepted(app.app_id, report):
        return True
    if not confirmer.warn_and_confirm(app, report, scan_result):
        return False
    if not confirmer.confirm_again(app):
        return False
    decisions.accept(app.app_id, Decisions.fingerprint(report), report)
    return True


def _inspect_and_deploy(app: AppInfo, prep: _Prepared, report: RiskReport, confirmer: Confirmer,
                        decisions: Decisions, status, plain_confirm: bool, done_message: str) -> Outcome:
    """Gate two on what was actually pulled or built, then deploy. Cleans up the checkout."""
    try:
        report.extend(_score(app, prep).findings)
        scan_result = _scan(prep, status)
        for path, sig in scan_result.infected:
            report.escalate(RiskLevel.RED, "clamav", f"{sig} in {Path(path).name}")
        if scan_result.error:
            report.escalate(RiskLevel.YELLOW, "clamav", f"scan did not complete: {scan_result.error}")

        if report.level >= DOUBLE_CONFIRM_FROM:
            if not _gate(app, report, scan_result, confirmer, decisions):
                return Outcome(installed=False, cancelled=True, report=report, scan=scan_result)
        elif plain_confirm and not confirmer.confirm_plain(app, report):
            return Outcome(installed=False, cancelled=True, report=report, scan=scan_result)

        _deploy(app, prep, status)
        return Outcome(installed=True, report=report, scan=scan_result, message=done_message)
    finally:
        if prep.checkout and prep.checkout.exists():
            shutil.rmtree(prep.checkout, ignore_errors=True)


def install(app: AppInfo, api: FlatsonarAPI, confirmer: Confirmer, decisions: Decisions,
            status: Status | None = None) -> Outcome:
    source = pick_source(app.sources)
    if source is None:
        return Outcome(installed=False, message="This app has no known install source.")
    if not fp.available():
        return Outcome(installed=False, message="flatpak is not installed on this system.")

    report = trust_report(app, source)

    # Gate 1: publisher + manifest, before any build runs.
    mpath: Path | None = None
    if source.kind == "manifest":
        if not source.manifest_url:
            return Outcome(installed=False, message="This app has no manifest to build from.")
        mpath = _fetch_manifest(app, source, api, status)
        # Checked here, not only by the crawler: the index can be a week old, and this
        # is the manifest that would actually be built. No dialog - there is nothing to
        # accept, flatpak-builder would only fail.
        problems = template_problems(mpath)
        if problems:
            return Outcome(installed=False, message=f"Not building {app.name}: {describe_template_problems(problems)}.")
        report.extend(audit_downloaded_manifest(app, mpath).findings)
        if not _gate(app, report, None, confirmer, decisions):
            return Outcome(installed=False, cancelled=True, report=report)

    scanner = start_scanner()
    try:
        prep = _build(app, source, mpath, status) if mpath is not None else _pull(app, source, api, status)
        prep.scanner = scanner
        # Gate 2: what we actually got.
        return _inspect_and_deploy(app, prep, report, confirmer, decisions, status,
                                   plain_confirm=True, done_message=f"{app.name} installed.")
    finally:
        if scanner is not None:
            scanner.close()


def update(app: AppInfo, installed: InstalledApp, api: FlatsonarAPI, confirmer: Confirmer,
           decisions: Decisions, status: Status | None = None) -> Outcome:
    """Update an installed app through the same gates as an install.

    Apps that came from a remote (Flathub, a project remote) are updated with ``flatpak
    update``: pull without deploying, inspect, warn only about what is new, deploy. Apps
    Flatsonar built from a manifest, or installed from a bundle, have no remote to update
    from, so they go through :func:`install` again (rebuild / re-download), which
    ``--reinstall``s over the old version.
    """
    if not fp.available():
        return Outcome(installed=False, message="flatpak is not installed on this system.")
    if installed.locally_built or not installed.origin:
        source = pick_source(app.sources)
        if source is None or source.kind not in ("manifest", "bundle"):
            return Outcome(installed=False, message=f"{app.name} was not installed from a remote and the index "
                                                    "has no manifest or bundle to update it from.")
        out = install(app, api, confirmer, decisions, status)
        if out.installed:
            out.message = f"{app.name} updated."
        return out

    # The publisher may have been re-assessed since the install (an id collision, a repo
    # that turned suspicious). Findings already accepted do not nag; new ones do.
    report = trust_report(app)
    scanner = start_scanner()
    try:
        prep = _pull_update(app, installed, status)
        prep.scanner = scanner
        return _inspect_and_deploy(app, prep, report, confirmer, decisions, status,
                                   plain_confirm=False, done_message=f"{app.name} updated.")
    finally:
        if scanner is not None:
            scanner.close()
