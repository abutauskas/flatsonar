from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceKind(str, enum.Enum):
    """How an app can be installed, in order of preference."""

    FLATHUB = "flathub"  # on the Flathub remote
    REMOTE = "remote"  # a third-party .flatpakrepo remote (gnome-nightly, kde, project-owned)
    BUNDLE = "bundle"  # a single-file .flatpak attached to a release
    MANIFEST = "manifest"  # only a flatpak-builder manifest exists: build locally


class App(Base):
    __tablename__ = "apps"

    app_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    summary: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    icon_url: Mapped[str | None] = mapped_column(String(1024))
    screenshots: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)

    license: Mapped[str | None] = mapped_column(String(255))
    is_oss: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Credit
    developer_name: Mapped[str | None] = mapped_column(String(255))
    upstream_url: Mapped[str | None] = mapped_column(String(1024))
    homepage: Mapped[str | None] = mapped_column(String(1024))
    # Column stays "sponsor_links" (renaming it needs a migration); the funding_links
    # attribute is what the rest of the codebase - and the API - actually uses.
    funding_links: Mapped[list[dict]] = mapped_column("sponsor_links", JSON, default=list)  # [{platform, url}]

    # Risk (precomputed from the manifest; the client recomputes at install time)
    risk_level: Mapped[str] = mapped_column(String(8), default="green", index=True)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    permissions: Mapped[list[dict]] = mapped_column(JSON, default=list)  # every finding, incl. green

    latest_version: Mapped[str | None] = mapped_column(String(64))
    stars: Mapped[int] = mapped_column(Integer, default=0)
    forks: Mapped[int] = mapped_column(Integer, default=0)
    repo_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repo_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    on_flathub: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flathub_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # upstream repo is archived

    # Publisher trust (see flatsonar_core.provenance): verified / reviewed / unverified / suspicious,
    # plus the findings that led there (namespace check, repo age, manifest audit, collisions).
    trust: Mapped[str] = mapped_column(String(12), default="unverified", index=True)
    trust_findings: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{check, level, reason}]

    # Is anyone still tending this (see flatsonar_core.maintenance): active / stale / abandoned.
    # Flathub's review process is itself a maintenance signal, so on_flathub apps are always
    # "active" here; this exists for the apps Flatsonar indexes straight from a repository,
    # where nothing else says whether the listing is a live project or a five-year-old fork.
    maintenance: Mapped[str] = mapped_column(String(12), default="active", index=True)
    maintenance_findings: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{check, level, reason}]

    # Did the crawler's last visit to this app's repository still parse its manifest?
    # False means the app's own listing is stale: it was building fine at some point
    # (that's how it got listed) but the repository has since changed in a way that
    # broke it - a YAML typo, a renamed file, a source moved out from under it.
    manifest_ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    manifest_error: Mapped[str | None] = mapped_column(String(512))

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sources: Mapped[list["InstallSource"]] = relationship(
        back_populates="app", cascade="all, delete-orphan", order_by="InstallSource.priority"
    )
    manifest: Mapped["ManifestRecord | None"] = relationship(
        back_populates="app", cascade="all, delete-orphan", uselist=False
    )


_KIND_PRIORITY = {SourceKind.FLATHUB: 0, SourceKind.REMOTE: 1, SourceKind.BUNDLE: 2, SourceKind.MANIFEST: 3}


class InstallSource(Base):
    __tablename__ = "install_sources"
    __table_args__ = (Index("ix_install_sources_app_kind", "app_id", "kind", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("apps.app_id", ondelete="CASCADE"), index=True)
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind, native_enum=False, length=16))
    priority: Mapped[int] = mapped_column(Integer, default=0)

    remote_name: Mapped[str | None] = mapped_column(String(64))  # e.g. flathub, gnome-nightly
    remote_url: Mapped[str | None] = mapped_column(String(1024))  # .flatpakrepo URL
    ref: Mapped[str | None] = mapped_column(String(255))  # app/org.foo.Bar/x86_64/stable
    bundle_url: Mapped[str | None] = mapped_column(String(1024))  # .flatpak download
    manifest_url: Mapped[str | None] = mapped_column(String(1024))  # raw manifest to build

    app: Mapped[App] = relationship(back_populates="sources")

    def __init__(self, **kw):
        super().__init__(**kw)
        if "priority" not in kw and self.kind is not None:
            self.priority = _KIND_PRIORITY[SourceKind(self.kind)]


class ManifestRecord(Base):
    __tablename__ = "manifests"

    app_id: Mapped[str] = mapped_column(ForeignKey("apps.app_id", ondelete="CASCADE"), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(1024))
    raw: Mapped[dict] = mapped_column(JSON)
    runtime: Mapped[str | None] = mapped_column(String(255))
    runtime_version: Mapped[str | None] = mapped_column(String(64))
    sdk: Mapped[str | None] = mapped_column(String(255))
    finish_args: Mapped[list[str]] = mapped_column(JSON, default=list)
    module_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    app: Mapped[App] = relationship(back_populates="manifest")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    found: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
