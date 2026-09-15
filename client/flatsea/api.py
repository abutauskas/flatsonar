"""Thin synchronous client for the Flatsea server. Call from worker threads."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_API = os.environ.get("FLATSEA_API", "http://localhost:8000")


@dataclass
class InstallSource:
    kind: str
    remote_name: str | None = None
    remote_url: str | None = None
    ref: str | None = None
    bundle_url: str | None = None
    manifest_url: str | None = None


@dataclass
class AppInfo:
    app_id: str
    name: str
    summary: str = ""
    icon_url: str | None = None
    categories: list[str] = field(default_factory=list)
    license: str | None = None
    developer_name: str | None = None
    risk_level: str = "green"
    on_flathub: bool = False
    flathub_verified: bool = False
    stars: int = 0
    has_sponsor: bool = False
    # detail-only
    description: str = ""
    screenshots: list[str] = field(default_factory=list)
    upstream_url: str | None = None
    homepage: str | None = None
    sponsor_links: list[dict[str, str]] = field(default_factory=list)
    risk_reasons: list[str] = field(default_factory=list)
    permissions: list[dict[str, str]] = field(default_factory=list)
    latest_version: str | None = None
    sources: list[InstallSource] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "AppInfo":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "sources"}
        info = cls(**known)
        info.sources = [InstallSource(**{k: v for k, v in s.items() if k in InstallSource.__dataclass_fields__})
                        for s in d.get("sources") or []]
        return info


@dataclass
class Page:
    items: list[AppInfo]
    total: int
    page: int
    per_page: int

    @property
    def has_more(self) -> bool:
        return self.page * self.per_page < self.total


class FlatseaAPI:
    def __init__(self, base_url: str = DEFAULT_API):
        self.base = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base, timeout=20.0, headers={"User-Agent": "Flatsea-client/0.1"})

    def list_apps(self, q: str | None = None, category: str | None = None, risk: str | None = None,
                  sort: str = "name", page: int = 1, per_page: int = 48, sponsor_only: bool = False) -> Page:
        params: dict[str, Any] = {"page": page, "per_page": per_page, "sort": sort}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        if risk:
            params["risk"] = risk
        if sponsor_only:
            params["sponsor_only"] = "true"
        r = self._client.get("/api/apps", params=params)
        r.raise_for_status()
        d = r.json()
        return Page([AppInfo.from_json(i) for i in d["items"]], d["total"], d["page"], d["per_page"])

    def get_app(self, app_id: str) -> AppInfo:
        r = self._client.get(f"/api/apps/{app_id}")
        r.raise_for_status()
        return AppInfo.from_json(r.json())

    def categories(self) -> list[tuple[str, int]]:
        r = self._client.get("/api/categories")
        r.raise_for_status()
        return [(c["name"], c["count"]) for c in r.json()]

    def stats(self) -> dict[str, Any]:
        r = self._client.get("/api/stats")
        r.raise_for_status()
        return r.json()

    def download(self, url: str, dest: str, progress=None) -> str:
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(got / total)
        return dest
