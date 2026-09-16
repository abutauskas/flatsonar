"""Catalogue queries shared by the JSON API (``/api``) and the website (``/``), so
both list, filter and count exactly the same apps."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import Select

from flatsonar_core import TrustLevel

from .models import App, CrawlRun, InstallSource, SourceKind

SORTS = {
    "name": App.name.asc(),
    "stars": App.stars.desc(),
    "updated": App.updated_at.desc(),
    "newest": App.first_seen.desc(),
}
RISKS = ("green", "yellow", "red")
TRUSTS = tuple(level.label for level in TrustLevel)


class BadFilter(ValueError):
    """A filter value the catalogue does not know (an unknown trust level)."""


def parse_trust(trust: str | None) -> list[str]:
    """``"verified,reviewed"`` -> ``["verified", "reviewed"]``; raises :class:`BadFilter`."""
    if not trust:
        return []
    levels = [t.strip().lower() for t in trust.split(",") if t.strip()]
    unknown = [t for t in levels if t not in TRUSTS]
    if unknown:
        raise BadFilter(f"unknown trust level(s): {', '.join(unknown)}")
    return levels


def apps_query(
    q: str | None = None,
    category: str | None = None,
    risk: str | None = None,
    trust: str | None = None,
    source: SourceKind | None = None,
    oss_only: bool = True,
    sponsor_only: bool = False,
    ids: list[str] | None = None,
    on_flathub: bool | None = None,
) -> Select:
    stmt = select(App)
    if oss_only:
        stmt = stmt.where(App.is_oss.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(App.name.ilike(like), App.summary.ilike(like), App.app_id.ilike(like),
                              App.developer_name.ilike(like)))
    if category:
        # categories is JSON; portable LIKE on its serialised form is good enough for now.
        stmt = stmt.where(func.cast(App.categories, String).ilike(f'%"{category}"%'))
    if risk:
        stmt = stmt.where(App.risk_level == risk)
    levels = parse_trust(trust)
    if levels:
        stmt = stmt.where(App.trust.in_(levels))
    if source:
        stmt = stmt.where(App.app_id.in_(select(InstallSource.app_id).where(InstallSource.kind == source)))
    if sponsor_only:
        stmt = stmt.where(func.cast(App.sponsor_links, String) != "[]")
    if ids is not None:
        stmt = stmt.where(App.app_id.in_([i.strip() for i in ids if i.strip()][:500]))
    if on_flathub is not None:
        stmt = stmt.where(App.on_flathub.is_(on_flathub))
    return stmt


def page_apps(db: Session, stmt: Select, sort: str = "name", page: int = 1, per_page: int = 48) -> tuple[list[App], int]:
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(SORTS[sort]).offset((page - 1) * per_page).limit(per_page)).all()
    return list(rows), total


def get_app(db: Session, app_id: str) -> App | None:
    return db.scalar(select(App).options(selectinload(App.sources), selectinload(App.manifest))
                     .where(App.app_id == app_id))


def category_counts(db: Session) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for cats in db.scalars(select(App.categories).where(App.is_oss.is_(True))):
        counter.update(cats or [])
    return counter.most_common()


def catalogue_stats(db: Session) -> dict[str, Any]:
    oss = select(App).where(App.is_oss.is_(True)).subquery()
    total = db.scalar(select(func.count()).select_from(oss)) or 0
    on_fh = db.scalar(select(func.count()).select_from(oss).where(oss.c.on_flathub.is_(True))) or 0
    with_sponsor = db.scalar(
        select(func.count()).select_from(oss).where(func.cast(oss.c.sponsor_links, String) != "[]")
    ) or 0
    by_risk = {level: count for level, count in
               db.execute(select(oss.c.risk_level, func.count()).group_by(oss.c.risk_level)).all()}
    by_trust = {level: count for level, count in
                db.execute(select(oss.c.trust, func.count()).group_by(oss.c.trust)).all()}
    runs = db.scalars(select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(10)).all()
    return {
        "apps": total,
        "on_flathub": on_fh,
        "off_flathub": total - on_fh,
        "with_sponsor": with_sponsor,
        "by_risk": by_risk,
        "by_trust": by_trust,
        "last_crawls": [
            {
                "source": r.source,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "found": r.found,
                "updated": r.updated,
                "skipped": r.skipped,
                "errors": len(r.errors or []),
            }
            for r in runs
        ],
    }
