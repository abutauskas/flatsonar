"""Decide how far to trust the publisher of one candidate.

Flatsonar indexes apps from anyone, verified or not. What it never does is let an
unverified app *look* verified. Every candidate leaves here with a
:class:`flatsonar_core.TrustLevel` and the findings behind it:

  * Flathub apps: ``verified`` if Flathub's verification programme says so, else
    ``reviewed`` (the manifest went through Flathub review and Flathub built it).
  * Off-Flathub apps: the app id is a namespace claim. ``io.github.alice.*`` must be
    hosted by alice, ``org.gnome.*`` by GNOME, and so on
    (:func:`flatsonar_core.check_ownership`). Custom domains can prove ownership
    the way Flathub lets them: a ``/.well-known/org.flathub.VerifiedApps.txt``
    file listing the id. Anything else is ``unverified``.
  * Any red finding (namespace impersonation, a build that opens its sandbox or
    pipes downloads into a shell) makes the app ``suspicious`` whoever publishes it.

Repository age, star count and a ``.flatpak`` bundle hosted somewhere other than
the project's forge add yellow notes. Yellow never changes the level on its own:
being new is not a crime, but the client shows every note before installing.

``assess`` also fills :class:`flatsonar_core.MaintenanceLevel` alongside trust: a
different question (is anyone still tending this?) answered from the same repo
signal (:func:`flatsonar_core.assess_maintenance`), so it rides along in one pass
rather than a second walk over every candidate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from flatsonar_core import (
    Finding,
    Ownership,
    OwnershipResult,
    RiskLevel,
    TrustLevel,
    assess_maintenance,
    audit_manifest,
    check_ownership,
    ownership_finding,
    trust_from_findings,
)
from flatsonar_core.provenance import domain_of

from ..models import SourceKind
from .base import Candidate, CrawlContext

if TYPE_CHECKING:
    from .forge import RepoInfo

log = logging.getLogger("flatsonar.crawler.trust")

WELL_KNOWN = "https://{domain}/.well-known/org.flathub.VerifiedApps.txt"
NEW_REPO_DAYS = 60
FEW_STARS = 5

# Where a forge puts release assets. A bundle anywhere else was uploaded by hand.
_ASSET_HOSTS = {
    "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "gitlab.com", "codeberg.org", "gitlab.gnome.org", "invent.kde.org", "gitlab.freedesktop.org", "framagit.org",
}


async def well_known_lists(ctx: CrawlContext, domain: str, app_id: str) -> bool:
    """Does ``https://<domain>/.well-known/org.flathub.VerifiedApps.txt`` list this id?"""
    try:
        r = await ctx.fetch(WELL_KNOWN.format(domain=domain))
    except Exception as exc:  # DNS failures and the like: not verified, not an error
        log.debug("well-known %s: %s", domain, exc)
        return False
    if not r.ok or not r.text:
        return False
    ids = {line.strip() for line in r.text.splitlines() if line.strip() and not line.startswith("#")}
    return app_id in ids


def repo_signals(repo: RepoInfo, now: datetime | None = None) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    out: list[Finding] = []
    if repo.created_at:
        age = (now - repo.created_at).days
        if age < NEW_REPO_DAYS and repo.stars < FEW_STARS:
            out.append(Finding("publisher:new-repo", RiskLevel.YELLOW,
                               f"repository is {age} day(s) old with {repo.stars} star(s): no track record yet"))
    return out


def source_signals(cand: Candidate, repo: RepoInfo | None) -> list[Finding]:
    out: list[Finding] = []
    forge_host = (urlsplit(repo.html_url).hostname or "").lower() if repo else ""
    for spec in cand.sources:
        if spec.kind is SourceKind.BUNDLE and spec.bundle_url:
            parts = urlsplit(spec.bundle_url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https":
                out.append(Finding("bundle:insecure-url", RiskLevel.RED,
                                   f"the .flatpak bundle is served over {parts.scheme or 'an unknown scheme'}"))
            elif host != forge_host and host not in _ASSET_HOSTS:
                out.append(Finding("bundle:offsite", RiskLevel.YELLOW,
                                   f"the .flatpak bundle is hosted on {host}, not on the project's forge"))
    return out


async def assess(ctx: CrawlContext, cand: Candidate, repo: RepoInfo | None = None) -> Candidate:
    """Fill ``cand.trust`` and ``cand.trust_findings``. Returns the same candidate."""
    findings: list[Finding] = []
    if cand.on_flathub:
        if cand.flathub_verified:
            base = TrustLevel.VERIFIED
            findings.append(Finding("publisher:flathub", RiskLevel.GREEN,
                                    "verified on Flathub: the developer proved they control this app id"))
        else:
            base = TrustLevel.REVIEWED
            findings.append(Finding("publisher:flathub", RiskLevel.GREEN,
                                    "on Flathub: manifest reviewed and built by Flathub; "
                                    "the developer has not verified the app id"))
    else:
        hosted_at = repo.html_url if repo else cand.upstream_url
        builds_from = cand.manifest.upstream_urls if cand.manifest else []
        own = check_ownership(cand.app_id, hosted_at, builds_from)
        if own.status is Ownership.UNKNOWN:
            domain = domain_of(cand.app_id)
            if domain and await well_known_lists(ctx, domain, cand.app_id):
                own = OwnershipResult(Ownership.VERIFIED,
                                      f"{domain} publishes a well-known file listing {cand.app_id}")
        findings.append(ownership_finding(own))
        base = TrustLevel.VERIFIED if own.status is Ownership.VERIFIED else TrustLevel.UNVERIFIED
        if repo:
            findings.extend(repo_signals(repo))

    if cand.manifest is not None:
        findings.extend(audit_manifest(cand.manifest))
    findings.extend(source_signals(cand, repo))

    cand.trust = trust_from_findings(base, findings)
    cand.trust_findings = findings

    archived = repo.archived if repo is not None else bool(cand.archived)
    pushed_at = repo.pushed_at if repo is not None else cand.repo_pushed_at
    maint = assess_maintenance(on_flathub=bool(cand.on_flathub), archived=archived, pushed_at=pushed_at)
    cand.maintenance = maint.level
    cand.maintenance_findings = maint.findings
    return cand
