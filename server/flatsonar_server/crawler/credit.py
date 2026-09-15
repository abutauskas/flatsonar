"""Work out who made an app from its upstream repository URL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_FORGES = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "gitlab.gnome.org": "gitlab",
    "invent.kde.org": "gitlab",
    "gitlab.freedesktop.org": "gitlab",
    "codeberg.org": "codeberg",
    "salsa.debian.org": "gitlab",
    "framagit.org": "gitlab",
    "sr.ht": "sourcehut",
    "git.sr.ht": "sourcehut",
    "bitbucket.org": "bitbucket",
}

_SSH_RE = re.compile(r"^(?:git@|ssh://git@)([^:/]+)[:/](.+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Upstream:
    url: str  # canonical https URL to the repo
    host: str
    forge: str | None
    owner: str | None
    repo: str | None

    @property
    def display_owner(self) -> str | None:
        return self.owner.lstrip("~") if self.owner else None


def normalise_repo_url(url: str | None) -> Upstream | None:
    if not url:
        return None
    url = url.strip()
    m = _SSH_RE.match(url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "git") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    path = parts.path.strip("/").removesuffix(".git")
    segs = [s for s in path.split("/") if s]
    forge = _FORGES.get(host)
    owner = segs[0] if segs else None
    repo = None
    if forge in ("github", "codeberg", "bitbucket", "sourcehut"):
        repo = segs[1] if len(segs) > 1 else None
        path = "/".join(segs[:2])
    elif forge == "gitlab":
        # GitLab allows nested groups: owner is the top group, repo the last segment.
        cut = segs.index("-") if "-" in segs else len(segs)
        segs = segs[:cut]
        repo = segs[-1] if len(segs) > 1 else None
        path = "/".join(segs)
    canonical = f"https://{host}/{path}" if path else f"https://{host}"
    return Upstream(url=canonical, host=host, forge=forge, owner=owner, repo=repo)


def developer_from(upstream: Upstream | None, fallback: str | None = None) -> str | None:
    """Prefer an explicit AppStream developer name; else the repo owner."""
    if fallback and fallback.strip():
        return fallback.strip()
    if upstream and upstream.display_owner:
        return upstream.display_owner
    return None
