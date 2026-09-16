from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .models import SourceKind


class SponsorLink(BaseModel):
    platform: str
    url: str


class Permission(BaseModel):
    arg: str
    level: str
    reason: str


class TrustFinding(BaseModel):
    """Why the publisher got the trust level it has: namespace check, repo age,
    manifest audit, id collisions. ``level`` uses the same green/yellow/red scale
    as permissions; the client lists non-green ones before installing."""

    check: str
    level: str
    reason: str


class InstallSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: SourceKind
    remote_name: str | None = None
    remote_url: str | None = None
    ref: str | None = None
    bundle_url: str | None = None
    manifest_url: str | None = None


class AppSummary(BaseModel):
    """What the store grid needs."""

    model_config = ConfigDict(from_attributes=True)

    app_id: str
    name: str
    summary: str
    icon_url: str | None
    categories: list[str]
    license: str | None
    developer_name: str | None
    risk_level: str
    on_flathub: bool
    flathub_verified: bool
    trust: str  # verified | reviewed | unverified | suspicious
    stars: int
    latest_version: str | None = None
    has_sponsor: bool = False


class AppDetail(AppSummary):
    description: str
    screenshots: list[str]
    upstream_url: str | None
    homepage: str | None
    sponsor_links: list[SponsorLink]
    risk_reasons: list[str]
    permissions: list[Permission]
    trust_findings: list[TrustFinding]
    forks: int
    repo_created_at: datetime | None
    repo_pushed_at: datetime | None
    is_oss: bool
    first_seen: datetime
    updated_at: datetime
    sources: list[InstallSourceOut]


class Page(BaseModel):
    items: list[AppSummary]
    total: int
    page: int
    per_page: int


class CategoryCount(BaseModel):
    name: str
    count: int


class Stats(BaseModel):
    apps: int
    on_flathub: int
    off_flathub: int
    with_sponsor: int
    by_risk: dict[str, int]
    by_trust: dict[str, int]
    last_crawls: list[dict]
