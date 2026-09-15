from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..models import App, CrawlRun, InstallSource, SourceKind
from ..schemas import AppDetail, AppSummary, CategoryCount, Page, Stats

router = APIRouter(prefix="/api", tags=["apps"])

_SORTS = {
    "name": App.name.asc(),
    "stars": App.stars.desc(),
    "updated": App.updated_at.desc(),
    "newest": App.first_seen.desc(),
}


def _summary(app: App) -> AppSummary:
    s = AppSummary.model_validate(app)
    s.has_sponsor = bool(app.sponsor_links)
    return s


@router.get("/apps", response_model=Page)
def list_apps(
    q: str | None = Query(None, description="Search name / summary / app id"),
    category: str | None = None,
    risk: str | None = Query(None, pattern="^(green|yellow|red)$"),
    source: SourceKind | None = Query(None, description="Only apps installable via this kind"),
    oss_only: bool = True,
    sponsor_only: bool = False,
    sort: str = Query("name", pattern="^(name|stars|updated|newest)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(48, ge=1, le=200),
    db: Session = Depends(get_session),
):
    stmt = select(App)
    if oss_only:
        stmt = stmt.where(App.is_oss.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(App.name.ilike(like), App.summary.ilike(like), App.app_id.ilike(like)))
    if category:
        # categories is JSON; portable LIKE on its serialised form is good enough for now.
        stmt = stmt.where(func.cast(App.categories, String).ilike(f'%"{category}"%'))
    if risk:
        stmt = stmt.where(App.risk_level == risk)
    if source:
        stmt = stmt.where(App.app_id.in_(select(InstallSource.app_id).where(InstallSource.kind == source)))
    if sponsor_only:
        stmt = stmt.where(func.cast(App.sponsor_links, String) != "[]")

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(_SORTS[sort]).offset((page - 1) * per_page).limit(per_page)).all()
    return Page(items=[_summary(a) for a in rows], total=total, page=page, per_page=per_page)


@router.get("/apps/{app_id}", response_model=AppDetail)
def get_app(app_id: str, db: Session = Depends(get_session)):
    app = db.scalar(select(App).options(selectinload(App.sources)).where(App.app_id == app_id))
    if app is None:
        raise HTTPException(404, f"{app_id} not found")
    d = AppDetail.model_validate(app)
    d.has_sponsor = bool(app.sponsor_links)
    return d


@router.get("/apps/{app_id}/manifest")
def get_manifest(app_id: str, db: Session = Depends(get_session)):
    app = db.get(App, app_id)
    if app is None or app.manifest is None:
        raise HTTPException(404, f"no manifest stored for {app_id}")
    m = app.manifest
    return {
        "app_id": app_id,
        "source_url": m.source_url,
        "runtime": m.runtime,
        "runtime_version": m.runtime_version,
        "sdk": m.sdk,
        "finish_args": m.finish_args,
        "module_count": m.module_count,
        "fetched_at": m.fetched_at,
        "manifest": m.raw,
    }


@router.get("/categories", response_model=list[CategoryCount])
def categories(db: Session = Depends(get_session)):
    counter: Counter[str] = Counter()
    for cats in db.scalars(select(App.categories).where(App.is_oss.is_(True))):
        counter.update(cats or [])
    return [CategoryCount(name=n, count=c) for n, c in counter.most_common()]


@router.get("/stats", response_model=Stats)
def stats(db: Session = Depends(get_session)):
    oss = select(App).where(App.is_oss.is_(True)).subquery()
    total = db.scalar(select(func.count()).select_from(oss)) or 0
    on_fh = db.scalar(select(func.count()).select_from(oss).where(oss.c.on_flathub.is_(True))) or 0
    with_sponsor = db.scalar(
        select(func.count()).select_from(oss).where(func.cast(oss.c.sponsor_links, String) != "[]")
    ) or 0
    by_risk = {
        level: count
        for level, count in db.execute(
            select(oss.c.risk_level, func.count()).group_by(oss.c.risk_level)
        ).all()
    }
    runs = db.scalars(select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(10)).all()
    return Stats(
        apps=total,
        on_flathub=on_fh,
        off_flathub=total - on_fh,
        with_sponsor=with_sponsor,
        by_risk=by_risk,
        last_crawls=[
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
    )

