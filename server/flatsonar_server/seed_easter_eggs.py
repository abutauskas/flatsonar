"""Seed Flatsonar's own catalogue entry: the store, listing itself.

Not a crawler source - there is exactly one row here and it is not discovered
by hunting a forge, it is declared. Reuses the same ``Candidate`` shape and
``upsert_candidate`` path every real app goes through, so trust and risk are
computed the same way for this listing as for anyone else's: the manifest is
parsed from the actual ``client/io.github.abutauskas.Flatsonar.json`` in this
repo, and its risk score comes out red, honestly, because it asks to talk to
org.freedesktop.Flatpak (the permission that lets it install other apps).

    python -m flatsonar_server.seed_easter_eggs

Wired into the site-build workflow so stars/pushed_at stay live and the entry
re-syncs on every deploy rather than going stale.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

from flatsonar_core import audit_manifest, check_ownership, ownership_finding, parse_manifest_text, trust_from_findings
from flatsonar_core.provenance import Ownership, TrustLevel

from .crawler.base import Candidate, SourceSpec, upsert_candidate
from .db import SessionLocal, init_db
from .models import SourceKind

log = logging.getLogger("flatsonar.seed")

REPO = "abutauskas/flatsonar"
REPO_URL = f"https://github.com/{REPO}"
BRANCH = "master"
MANIFEST_PATH = "client/io.github.abutauskas.Flatsonar.json"
RAW_MANIFEST_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{MANIFEST_PATH}"
ICON_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/client/data/icons/io.github.abutauskas.Flatsonar.svg"
BUNDLE_URL = f"{REPO_URL}/releases/download/v0.1.0/flatsonar.flatpak"

SUMMARY = "An F-Droid-style store for open-source Flatpak apps. Yes, it lists itself."

DESCRIPTION = """\
Flatsonar hunts down open-source Flatpak apps wherever they live - Flathub, GitHub, GitLab, \
Codeberg, project-owned remotes, .flatpak bundles attached to releases - and puts them in one \
store. Every listing credits the original creators and shows a Sponsor button where one exists.

Nothing here is submitted; it is found. Flatsonar re-derives publisher trust and sandbox risk \
itself instead of taking either on faith, and warns twice before installing anything that asks \
for broad permissions.

This listing is Flatsonar cataloguing itself, because an app store that hunts down Flatpak apps \
and happens to be one is exactly the kind of thing it would hunt down. Its own risk score is red: \
it asks to talk to org.freedesktop.Flatpak, the one permission that lets an app install other \
apps on your behalf. That is not a bug in the scoring - it is the honest cost of being an app \
store, and Flatsonar would rather show you that than quietly exempt itself.

- Client: GTK4 + libadwaita, packaged as the .flatpak bundle attached to this listing's release.
- Server: crawls Flathub, GitHub, GitLab and Codeberg for open-source Flatpak apps.
- License: GPL-3.0-or-later."""


def _fetch_repo_stats() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}", headers=headers, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        log.warning("could not fetch live repo stats, leaving stars/dates as they were: %s", exc)
        return {}


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def build_candidate() -> Candidate:
    repo_root = Path(__file__).resolve().parents[2]
    manifest_text = (repo_root / MANIFEST_PATH).read_text(encoding="utf-8")
    manifest = parse_manifest_text(manifest_text, MANIFEST_PATH)
    stats = _fetch_repo_stats()

    cand = Candidate(
        app_id=manifest.app_id,
        name="Flatsonar",
        summary=SUMMARY,
        description=DESCRIPTION,
        icon_url=ICON_URL,
        categories=["System", "PackageManager"],
        license="GPL-3.0-or-later",
        developer_name="abutauskas",
        upstream_url=REPO_URL,
        latest_version="0.1.0",
        stars=stats.get("stargazers_count"),
        forks=stats.get("forks_count"),
        repo_created_at=_parse_ts(stats.get("created_at")),
        repo_pushed_at=_parse_ts(stats.get("pushed_at")),
        on_flathub=False,
        flathub_verified=False,
        archived=False,
        manifest=manifest,
        manifest_url=RAW_MANIFEST_URL,
        sources=[
            SourceSpec(kind=SourceKind.BUNDLE, bundle_url=BUNDLE_URL),
            SourceSpec(kind=SourceKind.MANIFEST, manifest_url=RAW_MANIFEST_URL),
        ],
    )

    # Same trust derivation crawler.trust.assess() does for an off-Flathub candidate,
    # minus the network-only bits (well-known lookup, repo age) that do not apply:
    # ownership is settled directly (this repo demonstrably owns io.github.abutauskas).
    own = check_ownership(cand.app_id, cand.upstream_url, manifest.upstream_urls)
    findings = [ownership_finding(own), *audit_manifest(manifest)]
    base = TrustLevel.VERIFIED if own.status is Ownership.VERIFIED else TrustLevel.UNVERIFIED
    cand.trust = trust_from_findings(base, findings)
    cand.trust_findings = findings
    return cand


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db()
    cand = build_candidate()
    with SessionLocal() as db:
        app, created = upsert_candidate(db, cand)
        db.commit()
        log.info("%s %s (trust=%s, risk=%s)", "seeded" if created else "updated", app.app_id, app.trust,
                  app.risk_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
