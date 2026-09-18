"""Turn a GitHub-style ``FUNDING.yml`` (also used on Codeberg/GitLab by convention)
and AppStream donation URLs into a flat list of funding links."""

from __future__ import annotations

from typing import Any

import yaml

_PLATFORM_URL = {
    "github": "https://github.com/sponsors/{}",
    "patreon": "https://www.patreon.com/{}",
    "open_collective": "https://opencollective.com/{}",
    "ko_fi": "https://ko-fi.com/{}",
    "liberapay": "https://liberapay.com/{}",
    "issuehunt": "https://issuehunt.io/r/{}",
    "polar": "https://polar.sh/{}",
    "buy_me_a_coffee": "https://buymeacoffee.com/{}",
    "thanks_dev": "https://thanks.dev/{}",
    "community_bridge": "https://funding.communitybridge.org/projects/{}",
    "lfx_crowdfunding": "https://crowdfunding.lfx.linuxfoundation.org/projects/{}",
    "tidelift": "https://tidelift.com/funding/github/{}",
}


def _as_list(v: Any) -> list[str]:
    if v is None or v is False:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return [str(v)]


def parse_funding_yml(text: str) -> list[dict[str, str]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict[str, str]] = []
    for platform, value in data.items():
        key = str(platform).strip().lower()
        for handle in _as_list(value):
            handle = handle.strip()
            if not handle:
                continue
            if key == "custom":
                url = handle if handle.startswith("http") else f"https://{handle}"
                out.append({"platform": "custom", "url": url})
            elif key in _PLATFORM_URL:
                url = handle if handle.startswith("http") else _PLATFORM_URL[key].format(handle)
                out.append({"platform": key, "url": url})
            else:
                if handle.startswith("http"):
                    out.append({"platform": key, "url": handle})
    return _dedupe(out)


def donation_link(url: str | None) -> list[dict[str, str]]:
    if not url or not url.startswith("http"):
        return []
    host = url.split("/")[2].lower()
    platform = "custom"
    for key, tmpl in _PLATFORM_URL.items():
        if tmpl.split("/")[2].removeprefix("www.") in host:
            platform = key
            break
    return [{"platform": platform, "url": url}]


def _dedupe(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out = []
    for l in links:
        k = l["url"].rstrip("/").lower()
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def merge(*groups: list[dict[str, str]] | None) -> list[dict[str, str]]:
    return _dedupe([l for g in groups if g for l in g])
