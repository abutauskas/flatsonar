from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import catalogue
from ..db import get_session
from ..models import App, SourceKind
from ..schemas import AppDetail, AppSummary, CategoryCount, Page, Stats

router = APIRouter(prefix="/api", tags=["apps"])


def _summary(app: App) -> AppSummary:
    s = AppSummary.model_validate(app)
    s.has_funding = bool(app.funding_links)
    return s


@router.get("/apps", response_model=Page)
def list_apps(
    q: str | None = Query(None, description="Search name / summary / app id / developer"),
    category: str | None = None,
    risk: str | None = Query(None, pattern="^(green|yellow|red)$"),
    trust: str | None = Query(None, description="Publisher trust, comma-separated: verified,reviewed,unverified,suspicious"),
    source: SourceKind | None = Query(None, description="Only apps installable via this kind"),
    ids: str | None = Query(None, description="Only these app ids, comma-separated (the client's installed list)"),
    oss_only: bool = True,
    funding_only: bool = False,
    sort: str = Query("name", pattern="^(name|stars|updated|newest)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(48, ge=1, le=200),
    db: Session = Depends(get_session),
):
    try:
        stmt = catalogue.apps_query(q=q, category=category, risk=risk, trust=trust, source=source,
                                    oss_only=oss_only, funding_only=funding_only,
                                    ids=ids.split(",") if ids is not None else None)
    except catalogue.BadFilter as exc:
        raise HTTPException(422, str(exc))
    rows, total = catalogue.page_apps(db, stmt, sort, page, per_page, q=q)
    return Page(items=[_summary(a) for a in rows], total=total, page=page, per_page=per_page)


@router.get("/apps/{app_id}", response_model=AppDetail)
def get_app(app_id: str, db: Session = Depends(get_session)):
    app = catalogue.get_app(db, app_id)
    if app is None:
        raise HTTPException(404, f"{app_id} not found")
    d = AppDetail.model_validate(app)
    d.has_funding = bool(app.funding_links)
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
    return [CategoryCount(name=n, count=c) for n, c in catalogue.category_counts(db)]


@router.get("/stats", response_model=Stats)
def stats(db: Session = Depends(get_session)):
    return Stats(**catalogue.catalogue_stats(db))
