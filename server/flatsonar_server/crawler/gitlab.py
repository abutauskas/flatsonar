"""GitLab hunter. Works against gitlab.com by default and any self-hosted
instance (gitlab.gnome.org, invent.kde.org) via ``base_url``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

from .base import Candidate, CrawlContext, fan_out
from .forge import RepoInfo, candidates_from_repo, parse_timestamp

log = logging.getLogger("flatsonar.crawler.gitlab")

SEARCHES = ["topic=flatpak", "search=flatpak"]


class GitLabForge:
    name = "gitlab"

    def __init__(self, base_url: str = "https://gitlab.com", token: str | None = None):
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api/v4"
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def _repo_info(self, p: dict) -> RepoInfo:
        ns = p.get("namespace") or {}
        return RepoInfo(
            forge="gitlab",
            full_name=p["path_with_namespace"],
            html_url=p["web_url"],
            owner=(ns.get("path") or p["path_with_namespace"].split("/")[0]),
            default_branch=p.get("default_branch") or "main",
            description=p.get("description"),
            stars=int(p.get("star_count") or 0),
            forks=int(p.get("forks_count") or 0),
            created_at=parse_timestamp(p.get("created_at")),
            pushed_at=parse_timestamp(p.get("last_activity_at")),
            license_spdx=None,  # needs a per-project call; the metainfo usually has it
            archived=bool(p.get("archived")),
            fork=bool(p.get("forked_from_project")),
            avatar_url=p.get("avatar_url") or ns.get("avatar_url"),
            topics=list(p.get("topics") or p.get("tag_list") or []),  # tag_list: older GitLab instances
        )

    async def list_repos(self, ctx: CrawlContext, limit: int | None) -> AsyncIterator[RepoInfo]:
        budget = limit or 1000
        seen: set[str] = set()
        for search in SEARCHES:
            page = 1
            while len(seen) < budget:
                url = (f"{self.api}/projects?{search}&order_by=star_count&sort=desc&simple=false"
                       f"&archived=false&visibility=public&per_page=100&page={page}")
                data = await ctx.fetch_json(url, self._headers())
                if not data:
                    break
                for p in data:
                    full = p.get("path_with_namespace")
                    if not full or full in seen:
                        continue
                    seen.add(full)
                    # Belt and braces on top of visibility=public above: a token that can
                    # see private/internal projects (e.g. one scoped beyond public data)
                    # must never let one reach the public catalogue as a "candidate".
                    if p.get("visibility") != "public":
                        continue
                    yield self._repo_info(p)
                    if len(seen) >= budget:
                        return
                if len(data) < 100:
                    break
                page += 1

    def _pid(self, repo: RepoInfo) -> str:
        return quote(repo.full_name, safe="")

    async def load_tree(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        paths: list[str] = []
        page = 1
        while page <= 20:
            url = (f"{self.api}/projects/{self._pid(repo)}/repository/tree?recursive=true"
                   f"&ref={quote(repo.default_branch)}&per_page=100&page={page}")
            data = await ctx.fetch_json(url, self._headers())
            if not data:
                break
            paths += [e["path"] for e in data if e.get("type") == "blob"]
            if len(data) < 100:
                break
            page += 1
        repo.tree = paths
        if repo.license_spdx is None:
            proj = await ctx.fetch_json(f"{self.api}/projects/{self._pid(repo)}?license=true", self._headers())
            lic = (proj or {}).get("license") or {}
            key = lic.get("key") or lic.get("nickname")
            repo.license_spdx = _GITLAB_LICENSE_KEYS.get(key, None) if key else None
            repo.homepage = repo.homepage or (proj or {}).get("web_url")

    async def load_releases(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        data = await ctx.fetch_json(f"{self.api}/projects/{self._pid(repo)}/releases?per_page=5", self._headers())
        for rel in data or []:
            links = ((rel.get("assets") or {}).get("links")) or []
            for l in links:
                repo.release_assets.append((l.get("name", ""), l.get("direct_asset_url") or l.get("url", "")))
            if repo.release_assets:
                return

    def raw_url(self, repo: RepoInfo, path: str) -> str:
        return f"{self.base}/{repo.full_name}/-/raw/{repo.default_branch}/{path}"


# GitLab reports licenses by licensee key, not SPDX.
_GITLAB_LICENSE_KEYS = {
    "mit": "MIT", "apache-2.0": "Apache-2.0", "gpl-3.0": "GPL-3.0-only", "gpl-2.0": "GPL-2.0-only",
    "lgpl-3.0": "LGPL-3.0-only", "lgpl-2.1": "LGPL-2.1-only", "agpl-3.0": "AGPL-3.0-only",
    "mpl-2.0": "MPL-2.0", "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause",
    "isc": "ISC", "unlicense": "Unlicense", "cc0-1.0": "CC0-1.0", "epl-2.0": "EPL-2.0",
    "zlib": "Zlib", "wtfpl": "WTFPL", "0bsd": "0BSD", "bsl-1.0": "BSL-1.0", "artistic-2.0": "Artistic-2.0",
    "eupl-1.2": "EUPL-1.2", "cecill-2.1": "CECILL-2.1", "gpl-3.0+": "GPL-3.0-or-later",
}


class GitLabSource:
    name = "gitlab"

    def __init__(self, token: str | None = None, base_urls: tuple[str, ...] = ("https://gitlab.com",)):
        self.forges = [GitLabForge(u, token if "gitlab.com" in u else None) for u in base_urls]

    async def discover(self, ctx: CrawlContext, limit: int | None = None) -> AsyncIterator[Candidate]:
        n = 0
        for forge in self.forges:
            repos = [r async for r in forge.list_repos(ctx, limit)]

            async def worker(repo: RepoInfo, forge=forge) -> list[Candidate]:
                try:
                    return await candidates_from_repo(ctx, forge, repo)
                except Exception as exc:
                    log.warning("gitlab %s: %s", repo.full_name, exc)
                    return []

            async for cands in fan_out(repos, worker, ctx.concurrency):
                for cand in cands:
                    yield cand
                    n += 1
        log.info("gitlab: %d candidates", n)
