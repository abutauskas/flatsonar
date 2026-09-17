"""Shared crawler plumbing: polite HTTP with on-disk conditional caching, the
``Candidate`` record every source produces, and the upsert into the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flatsonar_core import Finding, Manifest, RiskLevel, TrustLevel, is_open_source, score_finish_args
from flatsonar_core.provenance import same_repo

from ..models import App, InstallSource, ManifestRecord, SourceKind
from ..settings import settings

log = logging.getLogger("flatsonar.crawler")


# --- HTTP ----------------------------------------------------------------------


class HttpCache:
    """Tiny ETag / Last-Modified cache on disk so repeated crawls mostly get 304s."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.root / hashlib.sha256(url.encode()).hexdigest()[:32]

    def load(self, url: str) -> dict[str, Any] | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def store(self, url: str, headers: httpx.Headers, body: str) -> None:
        entry = {"etag": headers.get("etag"), "last_modified": headers.get("last-modified"), "body": body}
        self._path(url).write_text(json.dumps(entry), encoding="utf-8")


class _HostLimiter:
    """Minimum spacing between requests to the same host, plus a global concurrency cap."""

    def __init__(self, min_interval: float, concurrency: int):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.sem = asyncio.Semaphore(concurrency)

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last.get(host, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = time.monotonic()


@dataclass
class Fetched:
    status: int
    text: str
    headers: httpx.Headers
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 or self.from_cache

    def json(self) -> Any:
        return json.loads(self.text)


class CrawlContext:
    def __init__(self, concurrency: int = 8, min_interval: float = 0.05):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json, text/plain, */*"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            http2=False,
        )
        self.cache = HttpCache(settings.crawl_cache_dir)
        self.limiter = _HostLimiter(min_interval, concurrency)
        self.settings = settings

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, url: str, headers: dict[str, str] | None = None, use_cache: bool = True) -> Fetched:
        host = urlsplit(url).hostname or ""
        hdrs = dict(headers or {})
        cached = self.cache.load(url) if use_cache else None
        if cached:
            if cached.get("etag"):
                hdrs["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                hdrs["If-Modified-Since"] = cached["last_modified"]

        async with self.limiter.sem:
            await self.limiter.wait(host)
            for attempt in range(3):
                try:
                    resp = await self.client.get(url, headers=hdrs)
                except httpx.ConnectError as exc:
                    # DNS failures and refused/unreachable connections are not transient:
                    # a dead or misconfigured host (a stale well-known domain, a repo's
                    # long-abandoned homepage, ...) will not resolve a second later, and
                    # retrying it 3x with backoff for every one of thousands of candidates
                    # is what turns "a few bad domains" into a crawl that visibly stalls.
                    log.debug("GET %s failed (not retrying, connect error): %s", url, exc)
                    return Fetched(0, "", httpx.Headers())
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        log.warning("GET %s failed: %s", url, exc)
                        return Fetched(0, "", httpx.Headers())
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if resp.status_code in (403, 429) and "rate limit" in resp.text.lower():
                    reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after")
                    delay = 60.0
                    if reset:
                        try:
                            v = float(reset)
                            delay = max(1.0, v - time.time()) if v > 1e6 else v
                        except ValueError:
                            pass
                    log.warning("rate limited by %s; sleeping %.0fs", host, min(delay, 900))
                    await asyncio.sleep(min(delay, 900))
                    continue
                break

        if resp.status_code == 304 and cached:
            return Fetched(200, cached["body"], resp.headers, from_cache=True)
        if resp.status_code == 200 and use_cache:
            self.cache.store(url, resp.headers, resp.text)
        return Fetched(resp.status_code, resp.text, resp.headers)

    async def fetch_json(self, url: str, headers: dict[str, str] | None = None) -> Any | None:
        r = await self.fetch(url, headers)
        if not r.ok:
            return None
        try:
            return r.json()
        except json.JSONDecodeError:
            return None


# --- candidates ----------------------------------------------------------------


@dataclass
class SourceSpec:
    kind: SourceKind
    remote_name: str | None = None
    remote_url: str | None = None
    ref: str | None = None
    bundle_url: str | None = None
    manifest_url: str | None = None


@dataclass
class Candidate:
    """Everything a source learned about one app. Fields left None are not overwritten
    on an existing row, so a weaker source (a forge crawl) can enrich a stronger one
    (Flathub) without clobbering it."""

    app_id: str
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    icon_url: str | None = None
    screenshots: list[str] | None = None
    categories: list[str] | None = None
    license: str | None = None
    is_oss: bool | None = None
    developer_name: str | None = None
    upstream_url: str | None = None
    homepage: str | None = None
    sponsor_links: list[dict[str, str]] | None = None
    latest_version: str | None = None
    stars: int | None = None
    forks: int | None = None
    repo_created_at: datetime | None = None
    repo_pushed_at: datetime | None = None
    on_flathub: bool | None = None
    flathub_verified: bool | None = None
    manifest: Manifest | None = None
    manifest_url: str | None = None
    sources: list[SourceSpec] = field(default_factory=list)
    # Filled by crawler.trust.assess(); None means "not assessed" and the row keeps what it had.
    trust: TrustLevel | None = None
    trust_findings: list[Finding] = field(default_factory=list)

    @property
    def trust_level(self) -> TrustLevel:
        return self.trust if self.trust is not None else TrustLevel.UNVERIFIED


class Skipped(Exception):
    """The candidate was deliberately not written (id collision with a different upstream)."""


class Source(Protocol):
    name: str

    def discover(self, ctx: CrawlContext, limit: int | None) -> AsyncIterator[Candidate]: ...


# --- upsert ------------------------------------------------------------------


def _finding_dict(f: Finding) -> dict[str, str]:
    return {"check": f.arg, "level": f.level.label, "reason": f.reason}


def _add_trust_finding(app: App, f: Finding) -> None:
    d = _finding_dict(f)
    if d not in (app.trust_findings or []):
        app.trust_findings = [*(app.trust_findings or []), d]


def _lookalike_findings(db: Session, cand: Candidate) -> list[Finding]:
    """An off-Flathub app that shares its *name* with a Flathub app but comes from a
    different repository is worth a note on the card. Names collide innocently all
    the time ("Calculator"), so this is a yellow note, not a verdict."""
    if cand.on_flathub or not cand.name:
        return []
    twins = db.scalars(
        select(App).where(App.on_flathub.is_(True), App.app_id != cand.app_id,
                          func.lower(App.name) == cand.name.strip().lower())
    ).all()
    out = []
    for twin in twins:
        if twin.upstream_url and same_repo(twin.upstream_url, cand.upstream_url):
            continue  # same project, different id (a Devel build, a rename)
        out.append(Finding("publisher:lookalike", RiskLevel.YELLOW,
                           f"same name as {twin.app_id} on Flathub but from a different repository"))
    return out


def _guard_collision(db: Session, app: App, cand: Candidate) -> None:
    """Two publishers, one app id. Decide who keeps the row.

    * Flathub always keeps it; a forge candidate may only enrich, and only when it is
      the same repository Flathub builds from (else an impostor could plant sponsor links).
    * Otherwise the candidate takes over only if it is strictly more trusted (e.g. the
      real ``io.github.alice`` repo turning up after a copy). Equal trust: first seen wins,
      the incumbent gets a note that someone else publishes the same id.
    """
    if not app.upstream_url or not cand.upstream_url or same_repo(app.upstream_url, cand.upstream_url):
        return
    incumbent = TrustLevel.from_label(app.trust or "unverified")
    if app.on_flathub and not cand.on_flathub:
        raise Skipped(f"{cand.app_id}: {cand.upstream_url} is not the repo Flathub builds ({app.upstream_url})")
    if cand.trust_level > incumbent and not app.on_flathub:
        log.info("%s: %s (%s) takes over from %s (%s)", cand.app_id, cand.upstream_url, cand.trust_level.label,
                 app.upstream_url, incumbent.label)
        for row in db.scalars(select(InstallSource).where(InstallSource.app_id == app.app_id)):
            db.delete(row)
        app.sponsor_links = []
        app.trust_findings = []
        db.flush()
        return
    _add_trust_finding(app, Finding("publisher:collision", RiskLevel.YELLOW,
                                    f"{cand.upstream_url} also publishes this app id"))
    raise Skipped(f"{cand.app_id}: id already taken by {app.upstream_url} ({incumbent.label}); "
                  f"candidate {cand.upstream_url} ({cand.trust_level.label}) skipped")


def upsert_candidate(db: Session, cand: Candidate) -> tuple[App, bool]:
    """Write a candidate into the DB. Returns (app, created). Raises :class:`Skipped`
    when a different publisher already owns the app id."""
    app = db.get(App, cand.app_id)
    created = app is None
    if created:
        app = App(app_id=cand.app_id, name=cand.name or cand.app_id)
        db.add(app)
    else:
        _guard_collision(db, app, cand)
    if not created and app.on_flathub and not cand.on_flathub:
        # A forge crawl found the upstream repo of an app Flathub already ships.
        # Flathub's data and permissions are authoritative; only take what it lacks.
        if cand.stars is not None:
            app.stars = max(app.stars or 0, cand.stars)
        if cand.forks is not None:
            app.forks = max(app.forks or 0, cand.forks)
        if cand.sponsor_links:
            merged = {(l["platform"], l["url"]): l for l in (app.sponsor_links or [])}
            for l in cand.sponsor_links:
                merged.setdefault((l["platform"], l["url"]), l)
            app.sponsor_links = list(merged.values())
        for attr in ("upstream_url", "homepage", "developer_name", "icon_url", "repo_created_at", "repo_pushed_at"):
            if not getattr(app, attr) and getattr(cand, attr):
                setattr(app, attr, getattr(cand, attr))
        return app, False

    for attr in (
        "name", "summary", "description", "icon_url", "screenshots", "categories", "license",
        "developer_name", "upstream_url", "homepage", "latest_version", "on_flathub", "flathub_verified",
        "repo_created_at", "repo_pushed_at",
    ):
        val = getattr(cand, attr)
        if val is not None and val != "" and val != []:
            setattr(app, attr, val)

    if cand.stars is not None:
        app.stars = max(app.stars or 0, cand.stars)
    if cand.forks is not None:
        app.forks = max(app.forks or 0, cand.forks)

    if cand.trust is not None:
        findings = [*cand.trust_findings, *_lookalike_findings(db, cand)]
        # Keep collision notes the incumbent collected; they describe the id, not this crawl.
        kept = [d for d in (app.trust_findings or []) if d.get("check") == "publisher:collision"]
        app.trust = cand.trust.label
        app.trust_findings = [*kept, *(_finding_dict(f) for f in findings)]

    if cand.sponsor_links:
        merged = {(l["platform"], l["url"]): l for l in (app.sponsor_links or [])}
        for l in cand.sponsor_links:
            merged.setdefault((l["platform"], l["url"]), l)
        app.sponsor_links = list(merged.values())

    if cand.is_oss is not None:
        app.is_oss = cand.is_oss
    elif app.license:
        app.is_oss = is_open_source(app.license)

    if cand.manifest is not None:
        m = cand.manifest
        report = score_finish_args(m.finish_args, m.app_id)
        app.risk_level = report.level.label
        app.risk_reasons = report.reasons
        app.permissions = [{"arg": f.arg, "level": f.level.label, "reason": f.reason} for f in report.findings]
        if app.manifest is None:
            app.manifest = ManifestRecord(app_id=app.app_id, source_url=cand.manifest_url or "", raw=m.raw)
        rec = app.manifest
        rec.source_url = cand.manifest_url or rec.source_url
        rec.raw = m.raw
        rec.runtime = m.runtime
        rec.runtime_version = m.runtime_version
        rec.sdk = m.sdk
        rec.finish_args = list(m.finish_args)
        rec.module_count = len(m.modules)
        if not app.upstream_url and m.upstream_urls:
            app.upstream_url = m.upstream_urls[0]

    existing = {s.kind: s for s in db.scalars(select(InstallSource).where(InstallSource.app_id == app.app_id))}
    for spec in cand.sources:
        row = existing.get(spec.kind)
        if row is None:
            row = InstallSource(app_id=app.app_id, kind=spec.kind)
            db.add(row)
        row.remote_name = spec.remote_name
        row.remote_url = spec.remote_url
        row.ref = spec.ref
        row.bundle_url = spec.bundle_url
        row.manifest_url = spec.manifest_url

    return app, created
