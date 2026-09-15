"""pull -> look at the files -> scan -> re-score -> warn (twice) -> deploy.

Runs on a worker thread. UI interaction goes through the ``Confirmer`` callback
object, which the window implements with libadwaita dialogs.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from flatsea_core import RiskLevel, RiskReport, metadata_to_finish_args, score_finish_args

from ..api import AppInfo, FlatseaAPI, InstallSource
from ..state import Decisions
from . import flatpak_cli as fp
from .scan import ScanResult, scan

log = logging.getLogger("flatsea.install")

# Anything at or above this level gets the two-step "Sure" flow.
DOUBLE_CONFIRM_FROM = RiskLevel.YELLOW


class Confirmer(Protocol):
    def warn_and_confirm(self, app: AppInfo, report: RiskReport, scan_result: ScanResult) -> bool:
        """First dialog: what is wrong. Returns True if the user pressed Sure."""

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


class _Prepared:
    """A pulled-but-not-deployed app plus where its files were checked out."""

    def __init__(self, source: InstallSource):
        self.source = source
        self.checkout: Path | None = None
        self.bundle_path: Path | None = None
        self.local_repo: Path | None = None
        self.ref: str | None = None


def _status(cb: Callable[[str], None] | None, msg: str) -> None:
    log.info(msg)
    if cb:
        cb(msg)


def _prepare(app: AppInfo, source: InstallSource, api: FlatseaAPI, status) -> _Prepared:
    prep = _Prepared(source)
    scan_root = fp.CACHE / "scan" / app.app_id

    if source.kind in ("flathub", "remote"):
        remote = source.remote_name or "flathub"
        if source.remote_url:
            _status(status, f"Adding remote {remote}…")
            fp.ensure_remote(remote, source.remote_url)
        ref = source.ref or f"app/{app.app_id}/{fp.default_arch()}/stable"
        _status(status, f"Downloading {app.name} (not installing yet)…")
        fp.pull(remote, ref, on_line=status)
        prep.ref = ref
        local = [r for r in fp.local_refs(app.app_id) if r.startswith(f"{remote}:")] or fp.local_refs(app.app_id)
        if local:
            _status(status, "Unpacking for inspection…")
            prep.checkout = fp.checkout(local[0], scan_root)

    elif source.kind == "bundle" and source.bundle_url:
        dest = fp.CACHE / "bundles" / f"{app.app_id}.flatpak"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _status(status, f"Downloading {app.name} bundle…")
        api.download(source.bundle_url, str(dest), progress=lambda f: status and status(f"Downloading… {f:.0%}"))
        prep.bundle_path = dest
        _status(status, "Importing bundle (not installing yet)…")
        fp.pull_bundle(dest, on_line=status)
        local = fp.local_refs(app.app_id)
        if local:
            prep.checkout = fp.checkout(local[0], scan_root)

    elif source.kind == "manifest" and source.manifest_url:
        mdir = fp.CACHE / "manifests" / app.app_id
        mdir.mkdir(parents=True, exist_ok=True)
        mpath = mdir / source.manifest_url.rsplit("/", 1)[-1]
        _status(status, "Fetching manifest…")
        api.download(source.manifest_url, str(mpath))
        _status(status, f"Building {app.name} from source with flatpak-builder (this can take a while)…")
        repo, ref = fp.build_from_manifest(mpath, app.app_id, on_line=status)
        prep.local_repo, prep.ref = repo, ref
        _status(status, "Unpacking for inspection…")
        prep.checkout = fp.checkout(ref, scan_root, repo=repo)
    else:
        raise fp.FlatpakError(f"no usable install source for {app.app_id}")
    return prep


def _score(app: AppInfo, prep: _Prepared) -> RiskReport:
    """Re-score from the real metadata when we have it; otherwise trust the index."""
    if prep.checkout:
        meta = fp.read_metadata(prep.checkout)
        if meta:
            return score_finish_args(metadata_to_finish_args(meta), app.app_id)
    return score_finish_args([p["arg"] for p in app.permissions], app.app_id)


def _deploy(prep: _Prepared, status) -> None:
    src = prep.source
    _status(status, "Installing…")
    if src.kind in ("flathub", "remote"):
        fp.deploy(src.remote_name or "flathub", prep.ref or "", on_line=status)
    elif src.kind == "bundle":
        assert prep.bundle_path
        fp.deploy_bundle(prep.bundle_path, on_line=status)
    elif src.kind == "manifest":
        assert prep.local_repo and prep.ref
        fp.deploy_local_build(prep.local_repo, prep.ref, on_line=status)


def install(app: AppInfo, api: FlatseaAPI, confirmer: Confirmer, decisions: Decisions,
            status: Callable[[str], None] | None = None) -> Outcome:
    source = pick_source(app.sources)
    if source is None:
        return Outcome(installed=False, message="This app has no known install source.")
    if not fp.available():
        return Outcome(installed=False, message="flatpak is not installed on this system.")

    prep = _prepare(app, source, api, status)
    try:
        report = _score(app, prep)
        scan_result = ScanResult(ran=False)
        if prep.checkout:
            _status(status, "Scanning with ClamAV…")
            scan_result = scan(prep.checkout)
        elif prep.bundle_path:
            scan_result = scan(prep.bundle_path)
        for path, sig in scan_result.infected:
            report.escalate(RiskLevel.RED, "clamav", f"{sig} in {Path(path).name}")
        if scan_result.error:
            report.escalate(RiskLevel.YELLOW, "clamav", f"scan did not complete: {scan_result.error}")

        fingerprint = Decisions.fingerprint(report)
        if report.level >= DOUBLE_CONFIRM_FROM and not decisions.already_accepted(app.app_id, fingerprint):
            if not confirmer.warn_and_confirm(app, report, scan_result):
                return Outcome(installed=False, cancelled=True, report=report, scan=scan_result)
            if not confirmer.confirm_again(app):
                return Outcome(installed=False, cancelled=True, report=report, scan=scan_result)
            decisions.accept(app.app_id, fingerprint, report)
        elif report.level < DOUBLE_CONFIRM_FROM:
            if not confirmer.confirm_plain(app, report):
                return Outcome(installed=False, cancelled=True, report=report, scan=scan_result)

        _deploy(prep, status)
        return Outcome(installed=True, report=report, scan=scan_result, message=f"{app.name} installed.")
    finally:
        if prep.checkout and prep.checkout.exists():
            shutil.rmtree(prep.checkout, ignore_errors=True)
