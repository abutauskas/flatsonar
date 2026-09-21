"""Independent Flatpak remotes: distros, projects and organisations that publish
their own browsable app catalogue outside of Flathub - GNOME's own nightly repo,
elementary's AppCenter, and so on. Off-Flathub apps found this way never had a
manifest sitting in a GitHub/GitLab/Codeberg repo for the forge crawler to find in
the first place; this is Flatsonar's only way of hunting them.

Unlike GitHub/GitLab/Codeberg, there is no search API for "find Flatpak remotes on
the internet" - these have to be known in advance (``settings.third_party_remotes``,
a comma-separated list of ``.flatpakrepo`` URLs). Given one, this reads the two
things every OSTree remote serves over plain HTTP with no client or auth needed:

  * ``<url>/summary`` - every ref (app id, arch, branch) the remote hosts, read by
    :func:`flatsonar_core.ostree_summary.parse_refs`
  * ``<url>/appstream/<arch>/appstream.xml.gz`` - name/summary/license/screenshots
    for every one of those apps, in one file

Only an app present in *both*, whose license clears :func:`is_open_source`, becomes
a candidate: a remote's summary carries no license information at all, so nothing
here gets listed on the strength of merely existing the way a code-hosted
manifest's own declared license can.

The appstream export above is a convenience some hosting setups add on top of the
OSTree protocol, not something every remote guarantees; a remote missing it is
still enumerated (so it's visible in logs) but yields no candidates yet. Reading
appstream data from a remote that doesn't export it needs walking the OSTree
commit -> tree -> file objects directly, which this does not attempt.
"""

from __future__ import annotations

import gzip
import logging
import re
from collections.abc import AsyncIterator

from flatsonar_core import is_open_source

from flatsonar_core.ostree_summary import SummaryRef, parse_refs

from ..models import SourceKind
from . import funding
from .appstream import MetaInfo, parse_collection
from .base import Candidate, CrawlContext, SourceSpec
from .credit import developer_from, normalise_repo_url
from .forge import parse_flatpakrepo
from .trust import assess

log = logging.getLogger("flatsonar.crawler.remotes")

_ARCHES = ("x86_64", "aarch64")
_BRANCH_PREFERENCE = ("stable", "master", "main")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "remote"


def _pick_ref(refs: list[SummaryRef]) -> SummaryRef:
    """One arch/branch per app id to advertise as the install ref: prefer the most
    universally-buildable arch, then the most stable-sounding branch, falling back
    to alphabetical order so the choice is at least deterministic."""
    def key(r: SummaryRef) -> tuple[int, int, str]:
        arch_rank = _ARCHES.index(r.arch) if r.arch in _ARCHES else len(_ARCHES)
        branch_rank = _BRANCH_PREFERENCE.index(r.branch) if r.branch in _BRANCH_PREFERENCE else len(_BRANCH_PREFERENCE)
        return (arch_rank, branch_rank, r.branch)
    return min(refs, key=key)


class RemoteCatalogue:
    """One independent remote, given its ``.flatpakrepo`` URL."""

    def __init__(self, flatpakrepo_url: str):
        self.flatpakrepo_url = flatpakrepo_url

    async def discover(self, ctx: CrawlContext) -> list[Candidate]:
        repo_resp = await ctx.fetch(self.flatpakrepo_url)
        if not repo_resp.ok:
            log.warning("remote %s: could not fetch .flatpakrepo", self.flatpakrepo_url)
            return []
        title, base_url = parse_flatpakrepo(repo_resp.text)
        if not base_url:
            log.warning("remote %s: no Url= in .flatpakrepo", self.flatpakrepo_url)
            return []
        base_url = base_url.rstrip("/")

        summary_bytes = await ctx.fetch_bytes(f"{base_url}/summary")
        if not summary_bytes:
            log.warning("remote %s: could not fetch summary", base_url)
            return []
        by_id: dict[str, list[SummaryRef]] = {}
        for ref in parse_refs(summary_bytes):
            if ref.kind == "app":
                by_id.setdefault(ref.app_id, []).append(ref)
        if not by_id:
            log.info("remote %s: no app refs in summary", base_url)
            return []

        catalogue: dict[str, MetaInfo] = {}
        for arch in _ARCHES:
            catalogue = await self._appstream_catalogue(ctx, base_url, arch)
            if catalogue:
                break
        if not catalogue:
            log.info("remote %s: %d app ids found, but no appstream catalogue at "
                     "the conventional path - nothing listable yet", base_url, len(by_id))
            return []

        remote_name = _slug(title or base_url)
        out: list[Candidate] = []
        for app_id, refs in by_id.items():
            meta = catalogue.get(app_id)
            if meta is None or not is_open_source(meta.license):
                continue
            ref = _pick_ref(refs)
            upstream = normalise_repo_url(meta.vcs)
            cand = Candidate(
                app_id=app_id,
                name=meta.name or app_id,
                summary=meta.summary or "",
                description=meta.description or "",
                categories=meta.categories,
                license=meta.license,
                is_oss=True,
                developer_name=developer_from(upstream, meta.developer_name),
                upstream_url=meta.vcs,
                icon_url=f"{base_url}/appstream/{arch}/icons/{meta.icon}" if meta.icon else None,
                homepage=meta.homepage,
                funding_links=funding.donation_link(meta.donation),
                latest_version=meta.latest_version,
                screenshots=meta.screenshots,
                sources=[SourceSpec(
                    kind=SourceKind.REMOTE, remote_name=remote_name, remote_url=self.flatpakrepo_url,
                    ref=f"app/{app_id}/{ref.arch}/{ref.branch}",
                )],
            )
            out.append(await assess(ctx, cand))
        log.info("remote %s: %d of %d app ids listable (open-source license found)",
                 base_url, len(out), len(by_id))
        return out

    async def _appstream_catalogue(self, ctx: CrawlContext, base_url: str, arch: str) -> dict[str, MetaInfo]:
        raw = await ctx.fetch_bytes(f"{base_url}/appstream/{arch}/appstream.xml.gz")
        if not raw:
            return {}
        try:
            text = gzip.decompress(raw).decode("utf-8", "replace")
        except OSError as exc:  # not actually gzip, or truncated
            log.warning("remote %s: appstream.xml.gz did not decompress: %s", base_url, exc)
            return {}
        return parse_collection(text)


class ThirdPartyRemotesSource:
    name = "remotes"

    def __init__(self, flatpakrepo_urls: list[str]):
        self.urls = flatpakrepo_urls

    async def discover(self, ctx: CrawlContext, limit: int | None = None) -> AsyncIterator[Candidate]:
        n = 0
        for url in self.urls:
            try:
                cands = await RemoteCatalogue(url).discover(ctx)
            except Exception as exc:  # one bad remote must not stop the others
                log.warning("remote %s: %s", url, exc)
                continue
            for cand in cands:
                yield cand
                n += 1
                if limit and n >= limit:
                    return
        log.info("remotes: %d candidates", n)
