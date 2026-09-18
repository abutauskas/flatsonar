"""Common logic for hunting Flatpak apps in upstream repositories on GitHub,
GitLab and Codeberg. A forge implementation lists repos and reads files; this
module turns one repo into ``Candidate``s.

Off-Flathub apps end up with one of these install sources:
  * bundle   - a ``.flatpak`` file attached to a release
  * remote   - a ``.flatpakrepo`` published by the project
  * manifest - only a flatpak-builder manifest exists, so Flatsonar builds it locally
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from flatsonar_core import Manifest, is_open_source, parse_manifest_text
from flatsonar_core.manifest import ManifestError, looks_like_manifest

from ..models import SourceKind
from . import funding
from .appstream import MetaInfo, parse_metainfo
from .base import Candidate, CrawlContext, SourceSpec
from .credit import developer_from, normalise_repo_url
from .trust import assess

log = logging.getLogger("flatsonar.crawler.forge")

# build-aux/flatpak/org.gnome.Foo.json, flatpak/com.foo.Bar.yml, org.foo.Bar.Devel.yaml ...
MANIFEST_NAME = re.compile(r"^[A-Za-z_][\w\-]*(\.[A-Za-z_][\w\-]*){2,}\.(json|ya?ml)$")
METAINFO_NAME = re.compile(r"\.(metainfo|appdata)\.xml(\.in(\.in)?)?$")
ICON_DIR = re.compile(r"icons/hicolor/(scalable|symbolic|\d+x\d+)/apps/")
_SKIP_DIRS = ("node_modules/", "vendor/", "third_party/", "subprojects/", ".flatpak-builder/")


@dataclass
class RepoInfo:
    forge: str
    full_name: str  # owner/repo (GitLab: full path)
    html_url: str
    owner: str
    default_branch: str
    description: str | None = None
    homepage: str | None = None
    stars: int = 0
    forks: int = 0
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    license_spdx: str | None = None
    archived: bool = False
    fork: bool = False
    avatar_url: str | None = None
    tree: list[str] = field(default_factory=list)  # file paths at default branch
    release_assets: list[tuple[str, str]] = field(default_factory=list)  # (name, url)


def parse_timestamp(value: str | None) -> datetime | None:
    """ISO 8601 as the forges emit it (``2024-01-02T03:04:05Z``) -> aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Forge(Protocol):
    name: str

    def list_repos(self, ctx: CrawlContext, limit: int | None) -> AsyncIterator[RepoInfo]: ...

    async def load_tree(self, ctx: CrawlContext, repo: RepoInfo) -> None: ...

    async def load_releases(self, ctx: CrawlContext, repo: RepoInfo) -> None: ...

    def raw_url(self, repo: RepoInfo, path: str) -> str: ...


def _wanted(path: str) -> bool:
    return not path.startswith(_SKIP_DIRS) and "/node_modules/" not in path


def find_manifests(tree: list[str]) -> list[str]:
    hits = [p for p in tree if _wanted(p) and MANIFEST_NAME.match(posixpath.basename(p))]
    # Prefer non-Devel / non-Nightly manifests, then shallower paths.
    return sorted(hits, key=lambda p: (".Devel." in p or ".Nightly." in p or ".Daily." in p, p.count("/"), p))


def find_metainfo(tree: list[str], app_id: str) -> str | None:
    cands = [p for p in tree if _wanted(p) and METAINFO_NAME.search(p)]
    for p in cands:
        if posixpath.basename(p).startswith(app_id):
            return p
    return cands[0] if cands else None


def find_icon(tree: list[str], app_id: str) -> str | None:
    best: tuple[int, str] | None = None
    for p in tree:
        if not ICON_DIR.search(p) or not posixpath.basename(p).startswith(app_id + "."):
            continue
        if p.endswith(".svg") and "symbolic" not in p:
            score = 10_000
        elif p.endswith(".png"):
            m = re.search(r"/(\d+)x\d+/", p)
            score = int(m.group(1)) if m else 0
        else:
            continue
        if re.search(r"\.(Devel|Nightly|Daily)\.", posixpath.basename(p)):
            score -= 5_000  # prefer the release icon over the development variant
        if best is None or score > best[0]:
            best = (score, p)
    return best[1] if best else None


def find_flatpakrepo(tree: list[str]) -> str | None:
    for p in tree:
        if p.endswith(".flatpakrepo"):
            return p
    return None


async def _read(ctx: CrawlContext, forge: Forge, repo: RepoInfo, path: str) -> str | None:
    r = await ctx.fetch(forge.raw_url(repo, path))
    return r.text if r.ok and r.text.strip() else None


async def _funding_links(ctx: CrawlContext, forge: Forge, repo: RepoInfo) -> list[dict[str, str]]:
    for path in (".github/FUNDING.yml", "FUNDING.yml", ".gitea/FUNDING.yml", ".gitlab/FUNDING.yml"):
        if path in repo.tree:
            text = await _read(ctx, forge, repo, path)
            if text:
                return funding.parse_funding_yml(text)
    return []


def _parse_flatpakrepo(text: str) -> tuple[str | None, str | None]:
    title = url = None
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "Title":
            title = v.strip()
        elif k.strip() == "Url":
            url = v.strip()
    return title, url


async def candidates_from_repo(ctx: CrawlContext, forge: Forge, repo: RepoInfo) -> list[Candidate]:
    if repo.fork:
        return []
    if repo.owner.lower() == "flathub":
        return []  # packaging repos: handled by the Flathub source
    await forge.load_tree(ctx, repo)
    manifest_paths = find_manifests(repo.tree)
    if not manifest_paths:
        return []

    await forge.load_releases(ctx, repo)
    funding_yml_links = await _funding_links(ctx, forge, repo)
    upstream = normalise_repo_url(repo.html_url)

    out: list[Candidate] = []
    seen_ids: set[str] = set()
    for path in manifest_paths:
        text = await _read(ctx, forge, repo, path)
        if not text or not looks_like_manifest(text):
            continue
        try:
            manifest: Manifest = parse_manifest_text(text, posixpath.basename(path))
        except ManifestError as exc:
            log.debug("%s:%s not a manifest: %s", repo.full_name, path, exc)
            continue
        app_id = manifest.app_id
        base_id = re.sub(r"\.(Devel|Nightly|Daily)$", "", app_id)
        if base_id in seen_ids:
            continue  # already got the stable manifest for this app
        seen_ids.add(base_id)
        manifest_url = forge.raw_url(repo, path)

        meta: MetaInfo | None = None
        mi_path = find_metainfo(repo.tree, base_id)
        if mi_path:
            mi_text = await _read(ctx, forge, repo, mi_path)
            if mi_text:
                meta = parse_metainfo(mi_text)

        license_ = (meta.license if meta and meta.license else None) or repo.license_spdx
        if not is_open_source(license_):
            log.debug("%s (%s): license %r not open source, skipping", base_id, repo.full_name, license_)
            continue

        sources: list[SourceSpec] = []
        for name, url in repo.release_assets:
            if name.endswith(".flatpak"):
                sources.append(SourceSpec(kind=SourceKind.BUNDLE, bundle_url=url))
                break
        fr = find_flatpakrepo(repo.tree)
        if fr:
            fr_text = await _read(ctx, forge, repo, fr)
            if fr_text:
                title, url = _parse_flatpakrepo(fr_text)
                if url:
                    sources.append(SourceSpec(
                        kind=SourceKind.REMOTE,
                        remote_name=re.sub(r"[^a-z0-9]+", "-", (title or repo.full_name).lower()).strip("-"),
                        remote_url=forge.raw_url(repo, fr),
                        ref=f"app/{base_id}/x86_64/stable",
                    ))
        sources.append(SourceSpec(kind=SourceKind.MANIFEST, manifest_url=manifest_url))

        icon_path = find_icon(repo.tree, base_id)
        name = (meta.name if meta else None) or repo.full_name.rsplit("/", 1)[-1]
        summary = (meta.summary if meta else None) or (repo.description or "")[:512]
        if name == "Example" and summary == "A summary":
            # A handful of repos (flatpak-builder-lint's test fixtures, and anything
            # shaped like it) ship many manifests with this exact literal placeholder
            # metadata to exercise a linter/parser - not real apps, just fixtures that
            # happen to look like real manifests to the file-name heuristic above.
            log.debug("%s:%s has placeholder Example/A summary metadata, skipping", repo.full_name, path)
            continue
        cand = Candidate(
            app_id=base_id,
            name=name,
            summary=summary,
            description=(meta.description if meta else None) or repo.description or "",
            icon_url=forge.raw_url(repo, icon_path) if icon_path else repo.avatar_url,
            screenshots=meta.screenshots if meta else [],
            categories=meta.categories if meta else [],
            license=license_,
            is_oss=True,
            developer_name=developer_from(upstream, meta.developer_name if meta else None),
            upstream_url=upstream.url if upstream else repo.html_url,
            homepage=(meta.homepage if meta else None) or repo.homepage,
            funding_links=funding.merge(funding_yml_links, funding.donation_link(meta.donation if meta else None)),
            latest_version=meta.latest_version if meta else None,
            stars=repo.stars,
            forks=repo.forks,
            repo_created_at=repo.created_at,
            repo_pushed_at=repo.pushed_at,
            archived=repo.archived,
            manifest=manifest,
            manifest_url=manifest_url,
            sources=sources,
        )
        out.append(await assess(ctx, cand, repo))
    return out
