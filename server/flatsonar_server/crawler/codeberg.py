"""Codeberg hunter (Gitea/Forgejo API). Also usable for any Forgejo instance."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

from .base import Candidate, CrawlContext
from .forge import RepoInfo, candidates_from_repo

log = logging.getLogger("flatsonar.crawler.codeberg")


class GiteaForge:
    name = "codeberg"

    def __init__(self, base_url: str = "https://codeberg.org", token: str | None = None):
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api/v1"
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"} if self.token else {}

    def _repo_info(self, r: dict) -> RepoInfo:
        owner = r.get("owner") or {}
        licenses = r.get("licenses") or []
        return RepoInfo(
            forge="codeberg",
            full_name=r["full_name"],
            html_url=r["html_url"],
            owner=owner.get("login") or r["full_name"].split("/")[0],
            default_branch=r.get("default_branch") or "main",
            description=r.get("description"),
            homepage=r.get("website") or None,
            stars=int(r.get("stars_count") or 0),
            license_spdx=licenses[0] if licenses else None,
            archived=bool(r.get("archived")),
            fork=bool(r.get("fork")),
            avatar_url=owner.get("avatar_url"),
        )

    async def list_repos(self, ctx: CrawlContext, limit: int | None) -> AsyncIterator[RepoInfo]:
        budget = limit or 1000
        seen: set[str] = set()
        for q, topic in (("flatpak", "true"), ("flatpak", "false")):
            page = 1
            while len(seen) < budget:
                url = (f"{self.api}/repos/search?q={q}&topic={topic}&sort=stars&order=desc"
                       f"&limit=50&page={page}")
                data = await ctx.fetch_json(url, self._headers())
                items = (data or {}).get("data") or []
                if not items:
                    break
                for r in items:
                    if r["full_name"] in seen:
                        continue
                    seen.add(r["full_name"])
                    yield self._repo_info(r)
                    if len(seen) >= budget:
                        return
                if len(items) < 50:
                    break
                page += 1

    async def load_tree(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        url = f"{self.api}/repos/{repo.full_name}/git/trees/{quote(repo.default_branch)}?recursive=true&per_page=10000"
        data = await ctx.fetch_json(url, self._headers())
        repo.tree = [e["path"] for e in (data or {}).get("tree") or [] if e.get("type") == "blob"]

    async def load_releases(self, ctx: CrawlContext, repo: RepoInfo) -> None:
        data = await ctx.fetch_json(f"{self.api}/repos/{repo.full_name}/releases?limit=5", self._headers())
        for rel in data or []:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for a in rel.get("assets") or []:
                repo.release_assets.append((a.get("name", ""), a.get("browser_download_url", "")))
            if repo.release_assets:
                return

    def raw_url(self, repo: RepoInfo, path: str) -> str:
        return f"{self.base}/{repo.full_name}/raw/branch/{repo.default_branch}/{path}"


class CodebergSource:
    name = "codeberg"

    def __init__(self, token: str | None = None):
        self.forge = GiteaForge(token=token)

    async def discover(self, ctx: CrawlContext, limit: int | None = None) -> AsyncIterator[Candidate]:
        n = 0
        async for repo in self.forge.list_repos(ctx, limit):
            try:
                for cand in await candidates_from_repo(ctx, self.forge, repo):
                    yield cand
                    n += 1
            except Exception as exc:
                log.warning("codeberg %s: %s", repo.full_name, exc)
        log.info("codeberg: %d candidates", n)
