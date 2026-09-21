"""GitHub hunter. Finds repositories that ship a flatpak-builder manifest via
(1) the ``flatpak`` topic and (2) code search for manifest-looking files, then
hands each repo to :mod:`forge`. Needs ``GITHUB_TOKEN``: code search is
auth-only and anonymous limits are too small to be useful."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

from .base import Candidate, CrawlContext, fan_out
from .forge import RepoInfo, candidates_from_repo, parse_timestamp

log = logging.getLogger("flatsonar.crawler.github")

API = "https://api.github.com"

REPO_QUERIES = [
    "topic:flatpak",
    "topic:flatpak-app",
    "topic:flatpak-builder",
    "topic:flatpak-manifest",
    "topic:libadwaita",
    "topic:gtk4 flatpak",
    "topic:gtk3 flatpak",
    "topic:kirigami flatpak",
    "topic:electron flatpak",
]
# Legacy code search: each query is capped at 1000 results, so vary the extension.
CODE_QUERIES = [
    '"app-id" "finish-args" "modules" extension:json',
    '"app-id" "finish-args" "modules" extension:yml',
    '"app-id" "finish-args" "modules" extension:yaml',
    # Path-based: catches manifests that never literally contain "app-id" (e.g. the
    # legacy `id:` key - parse_manifest_text already accepts data.get("app-id") or
    # data.get("id")) or "finish-args"/"modules" as bare strings. Only needs to find
    # the repo, not the exact file: find_manifests()/candidates_from_repo() re-validate
    # the tree and manifest content independently of which query found the repo, so a
    # path-based false positive costs a little crawl time, not bad data.
    'path:flatpak extension:json',
    'path:flatpak extension:yml',
    'path:flatpak extension:yaml',
]
# WATCH: GitHub's /search/code responses carry a Link header pointing at a (currently
# 404) blog post about search deprecation, sunset date 2026-09-27. GitHub staff haven't
# confirmed scope - it looks like it may only affect the sort/order params, which this
# code doesn't use - but if code search stops working outright after that date, start here.


class GitHubForge:
    name = "github"

    def __init__(self, token: str | None):
        self.token = token

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _repo_info(self, r: dict) -> RepoInfo:
        lic = (r.get("license") or {}).get("spdx_id")
        return RepoInfo(
            forge="github",
            full_name=r["full_name"],
            html_url=r["html_url"],
            owner=r["owner"]["login"],
            default_branch=r.get("default_branch") or "main",
            description=r.get("description"),
            homepage=r.get("homepage") or None,
            stars=int(r.get("stargazers_count") or 0),
            forks=int(r.get("forks_count") or 0),
            created_at=parse_timestamp(r.get("created_at")),
            pushed_at=parse_timestamp(r.get("pushed_at")),
            license_spdx=None if lic in (None, "NOASSERTION") else lic,
            archived=bool(r.get("archived")),
            fork=bool(r.get("fork")),
            avatar_url=(r.get("owner") or {}).get("avatar_url"),
            topics=list(r.get("topics") or []),
        )

    async def _search_repos(self, ctx: CrawlContext, query: str, budget: int) -> AsyncIterator[dict]:
        page = 1
        got = 0
        while got < budget and page <= 10:
            url = f"{API}/search/repositories?q={quote(query)}&sort=stars&order=desc&per_page=100&page={page}"
            data = await ctx.fetch_json(url, self._headers())
            items = (data or {}).get("items") or []
            if not items:
                return
            for it in items:
                yield it
                got += 1
                if got >= budget:
                    return
            page += 1

    async def _search_code(self, ctx: CrawlContext, query: str, budget: int) -> AsyncIterator[str]:
        """Yields repo full names from code search hits."""
        page = 1
        got = 0
        seen: set[str] = set()
        while got < budget and page <= 10:
            url = f"{API}/search/code?q={quote(query)}&per_page=100&page={page}"
            data = await ctx.fetch_json(url, self._headers())
            items = (data or {}).get("items") or []
            if not items:
                return
            for it in items:
                full = (it.get("repository") or {}).get("full_name")
                if full and full not in seen:
                    seen.add(full)
                    yield full
                    got += 1
            page += 1

    async def _get_repo(self, ctx: CrawlContext, full_name: str) -> RepoInfo | None:
        data = await ctx.fetch_json(f"{API}/repos/{full_name}", self._headers())
        return self._repo_info(data) if data and "full_name" in data else None

    async def list_repos(self, ctx: CrawlContext, limit: int | None) -> AsyncIterator[RepoInfo]:
        # Topic search and code search each get their own budget. The `flatpak` topic
        # is opt-in and most flatpak-shipping repos never add it, so code search is the
        # higher-recall half of the hunt, not a fallback - it must run in full even when
        # topic search alone already reached `budget` distinct repos. Sharing one pool
        # meant code search could go entirely unused on an ordinary crawl, and only gets
        # worse as the tagged corpus grows.
        budget = limit or 2000
        seen: set[str] = set()
        by_topic = 0
        for q in REPO_QUERIES:
            async for r in self._search_repos(ctx, q, budget):
                if r["full_name"] in seen:
                    continue
                seen.add(r["full_name"])
                yield self._repo_info(r)
                by_topic += 1
                if by_topic >= budget:
                    break
            if by_topic >= budget:
                break
        if not self.token:
            log.warning("no GITHUB_TOKEN: skipping code search")
            return
        by_code = 0
        for q in CODE_QUERIES:
            async for full in self._search_code(ctx, q, budget):
                if full in seen:
                    continue
                seen.add(full)
                repo = await self._get_repo(ctx, full)
                if repo:
                    yield repo
                by_code += 1
                if by_code >= budget:
                    break
            if by_code >= budget:
                break

    async def load_tree(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        url = f"{API}/repos/{repo.full_name}/git/trees/{quote(repo.default_branch)}?recursive=1"
        data = await ctx.fetch_json(url, self._headers())
        repo.tree = [e["path"] for e in (data or {}).get("tree") or [] if e.get("type") == "blob"]

    async def load_releases(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        data = await ctx.fetch_json(f"{API}/repos/{repo.full_name}/releases?per_page=5", self._headers())
        for rel in data or []:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for a in rel.get("assets") or []:
                repo.release_assets.append((a.get("name", ""), a.get("browser_download_url", "")))
            if repo.release_assets:
                return

    def raw_url(self, repo: RepoInfo, path: str) -> str:
        return f"https://raw.githubusercontent.com/{repo.full_name}/{repo.default_branch}/{path}"


class GitHubSource:
    name = "github"

    def __init__(self, token: str | None):
        self.forge = GitHubForge(token)

    async def discover(self, ctx: CrawlContext, limit: int | None = None) -> AsyncIterator[Candidate]:
        repos = [r async for r in self.forge.list_repos(ctx, limit)]

        async def worker(repo: RepoInfo) -> list[Candidate]:
            try:
                return await candidates_from_repo(ctx, self.forge, repo)
            except Exception as exc:
                log.warning("github %s: %s", repo.full_name, exc)
                return []

        n = 0
        async for cands in fan_out(repos, worker, ctx.concurrency):
            for cand in cands:
                yield cand
                n += 1
        log.info("github: %d candidates", n)
