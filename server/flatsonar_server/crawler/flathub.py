"""Flathub: the bulk of the catalogue.

Per app we hit three things:
  * ``/api/v2/appstream/{id}``   name, summary, license, icon, screenshots, urls, verification
  * ``/api/v2/summary/{id}``     the permissions actually deployed on the remote
  * ``github.com/flathub/{id}``  the manifest, for ``sources`` -> upstream repo credit

The deployed permissions win over the manifest's ``finish-args`` when both exist.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from flatsonar_core import Manifest, is_open_source, parse_manifest_text
from flatsonar_core.manifest import ManifestError

from ..models import SourceKind
from . import funding
from .base import Candidate, CrawlContext, SourceSpec, fan_out
from .credit import developer_from, normalise_repo_url
from .trust import assess

log = logging.getLogger("flatsonar.crawler.flathub")

API = "https://flathub.org/api/v2"
REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
_TAG = re.compile(r"<[^>]+>")


def permissions_to_finish_args(perms: dict[str, Any] | None) -> list[str]:
    """Flathub's summary ``permissions`` block -> the equivalent ``finish-args``."""
    if not perms:
        return []
    out: list[str] = []
    for kind, key in (("share", "shared"), ("socket", "sockets"), ("device", "devices"),
                      ("filesystem", "filesystems"), ("allow", "features")):
        for v in perms.get(key) or []:
            out.append(f"--{kind}={v}")
    for bus, prefix in (("session-bus", ""), ("system-bus", "system-")):
        b = perms.get(bus) or {}
        for name in b.get("talk") or []:
            out.append(f"--{prefix}talk-name={name}")
        for name in b.get("own") or []:
            out.append(f"--{prefix}own-name={name}")
    for k, v in (perms.get("environment") or {}).items():
        out.append(f"--env={k}={v}")
    for k in perms.get("unset-environment") or []:
        out.append(f"--unset-env={k}")
    return out


def _html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"</p>\s*<p>", "\n\n", s)
    s = re.sub(r"<li>", "- ", s)
    s = _TAG.sub("", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


class FlathubSource:
    name = "flathub"

    def __init__(self, concurrency: int = 8):
        self.concurrency = concurrency

    async def _manifest(self, ctx: CrawlContext, app_id: str) -> tuple[Manifest | None, str | None]:
        for branch in ("master", "main"):
            for ext in ("json", "yml", "yaml"):
                url = f"https://raw.githubusercontent.com/flathub/{app_id}/{branch}/{app_id}.{ext}"
                r = await ctx.fetch(url)
                if r.ok and r.text.strip():
                    try:
                        return parse_manifest_text(r.text, f"{app_id}.{ext}"), url
                    except ManifestError as exc:
                        log.debug("manifest for %s unparsable: %s", app_id, exc)
                        return None, url
        return None, None

    async def one(self, ctx: CrawlContext, app_id: str) -> Candidate | None:
        meta = await ctx.fetch_json(f"{API}/appstream/{app_id}")
        if not meta or meta.get("type") not in (None, "desktop-application", "console-application", "desktop"):
            return None
        summary_json, (manifest, manifest_url) = await asyncio.gather(
            ctx.fetch_json(f"{API}/summary/{app_id}"), self._manifest(ctx, app_id)
        )

        license_ = meta.get("project_license")
        is_oss = bool(meta.get("is_free_license")) if meta.get("is_free_license") is not None else is_open_source(license_)
        if not is_oss and not is_open_source(license_):
            # Not in scope: proprietary or unknown license.
            return Candidate(app_id=app_id, name=meta.get("name"), license=license_, is_oss=False, on_flathub=True)

        deployed = permissions_to_finish_args(((summary_json or {}).get("metadata") or {}).get("permissions"))
        if deployed:
            if manifest is None:
                manifest = Manifest(app_id=app_id, finish_args=deployed, raw={"finish-args": deployed})
            else:
                manifest.finish_args = deployed
        if manifest is not None and summary_json:
            md = summary_json.get("metadata") or {}
            manifest.runtime = manifest.runtime or md.get("runtimeName")
            manifest.sdk = manifest.sdk or md.get("sdk")

        urls = meta.get("urls") or {}
        upstream = normalise_repo_url(urls.get("vcs_browser")) or (
            normalise_repo_url(manifest.upstream_urls[0]) if manifest and manifest.upstream_urls else None
        )
        md = meta.get("metadata") or {}
        releases = meta.get("releases") or []
        screenshots = []
        for shot in meta.get("screenshots") or []:
            sizes = shot.get("sizes") or []
            if sizes:
                best = max(sizes, key=lambda s: int(s.get("width") or 0))
                if best.get("src"):
                    screenshots.append(best["src"])

        bundle_ref = (meta.get("bundle") or {}).get("value") or f"app/{app_id}/x86_64/stable"
        return Candidate(
            app_id=app_id,
            name=meta.get("name") or app_id,
            summary=meta.get("summary") or "",
            description=_html_to_text(meta.get("description")),
            icon_url=meta.get("icon"),
            screenshots=screenshots,
            categories=meta.get("categories") or [],
            license=license_,
            is_oss=True,
            developer_name=developer_from(upstream, meta.get("developer_name")),
            upstream_url=upstream.url if upstream else urls.get("vcs_browser"),
            homepage=urls.get("homepage"),
            funding_links=funding.donation_link(urls.get("donation")),
            latest_version=(releases[0].get("version") if releases else None),
            on_flathub=True,
            flathub_verified=bool(md.get("flathub::verification::verified")),
            manifest=manifest,
            manifest_url=manifest_url,
            sources=[SourceSpec(kind=SourceKind.FLATHUB, remote_name="flathub", remote_url=REMOTE_URL, ref=bundle_ref)],
        )

    async def discover(self, ctx: CrawlContext, limit: int | None = None) -> AsyncIterator[Candidate]:
        ids = await ctx.fetch_json(f"{API}/appstream")
        if not isinstance(ids, list):
            log.error("could not list Flathub apps")
            return
        if limit:
            ids = ids[:limit]
        log.info("flathub: %d app ids", len(ids))

        async def worker(app_id: str) -> Candidate | None:
            try:
                cand = await self.one(ctx, app_id)
                if cand is not None:
                    await assess(ctx, cand)
                return cand
            except Exception as exc:  # keep the crawl going
                log.warning("flathub %s: %s", app_id, exc)
                return None

        async for cand in fan_out(ids, worker, self.concurrency):
            if cand is not None:
                yield cand
