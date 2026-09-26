"""HTML routes: landing page, catalogue, app pages, about, sitemap and feed.

Everything here reads through :mod:`flatsonar_server.catalogue`, the same queries the
JSON API uses, so the website never lists an app the client would not."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalogue
from ..db import get_session
from ..models import App, SourceKind
from ..settings import settings

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
router = APIRouter(include_in_schema=False)

SITE = {
    "name": "Flatsonar",
    "tagline": "Open-source Flatpak apps, hunted from everywhere",
    "repo": "https://github.com/abutauskas/flatsonar",
    "issues": "https://github.com/abutauskas/flatsonar/issues",
    "version": "1.1.0",
}

# Wording shared with the desktop client (client/flatsonar/widgets.py).
RISK_TEXT = {"green": "Sandboxed", "yellow": "Broad permissions", "red": "Extensive permissions"}
RISK_TIP = {
    "green": "Only ordinary sandbox permissions.",
    "yellow": "Asks for permissions that weaken the sandbox. Flatsonar warns before installing.",
    "red": "Asks for permissions that reach well outside the sandbox: full filesystem or bus access, "
           "for example. Plenty of legitimate apps need this to do their job. Flatsonar warns twice before "
           "installing, since nobody has reviewed whether the app actually needs it.",
}
TRUST_TEXT = {"verified": "Verified creator", "reviewed": "Flathub reviewed", "unverified": "Unverified publisher",
              "suspicious": "Suspicious publisher"}
TRUST_TIP = {
    "verified": "The creator demonstrably controls this app id: Flathub verification, the hosting account owns "
                "the namespace, or a well-known file on their domain.",
    "reviewed": "On Flathub: the manifest was reviewed and built on Flathub's infrastructure, but the developer "
                "has not verified ownership of the app id.",
    "unverified": "Nobody has confirmed that the publisher controls this app id. Flatsonar warns before installing.",
    "suspicious": "Something concrete is wrong: the id claims a namespace this repository does not own, or the "
                  "build does things a build should not. Flatsonar warns twice.",
}
MAINTENANCE_TEXT = {"active": "Actively maintained", "stale": "Quiet for a while", "abandoned": "Looks abandoned"}
MAINTENANCE_TIP = {
    "active": "Recent activity on the upstream repository, or built and reviewed by Flathub.",
    "stale": "No commits in a year or more. Might still work fine; nobody has touched it in a while.",
    "abandoned": "The upstream repository is archived, or has had no commits in several years.",
}
FUNDING_LABEL = {
    "github": "GitHub Sponsors", "patreon": "Patreon", "ko_fi": "Ko-fi", "liberapay": "Liberapay",
    "open_collective": "Open Collective", "buy_me_a_coffee": "Buy Me a Coffee", "custom": "Donate",
}
SORT_LABELS = [("name", "Name"), ("stars", "Most starred"), ("updated", "Recently updated"), ("newest", "Newest")]
RISK_FILTERS = [("", "Any risk"), ("green", "Sandboxed only"), ("yellow", "Broad permissions"), ("red", "Extensive permissions")]
TRUST_FILTERS = [("", "Any publisher"), ("verified", "Verified creators"), ("verified,reviewed", "Verified or Flathub"),
                 ("unverified,suspicious", "Unverified only")]
MAINTENANCE_FILTERS = [("", "Any maintenance"), ("active", "Actively maintained"), ("stale,abandoned", "Quiet or abandoned")]
WHERE_FILTERS = [("", "Anywhere"), ("flathub", "On Flathub"), ("hunted", "Outside Flathub")]
PER_PAGE = 36


# --- template helpers -------------------------------------------------------------------


def paragraphs(text: str | None) -> Markup:
    """Plain text with blank-line paragraphs and ``- `` bullets (what the crawler stores)
    -> escaped HTML."""
    if not text:
        return Markup("")
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        if lines and all(l.lstrip().startswith(("- ", "* ", "• ")) for l in lines):
            items = "".join(f"<li>{escape(l.lstrip()[2:].strip())}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{escape(' '.join(l.strip() for l in lines))}</p>")
    return Markup("".join(out))


def funding_label(link: dict[str, str]) -> str:
    """One app can list several links on the same platform (e.g. FUNDING.yml's
    ``github:`` accepts up to four usernames); tag each with its handle so they
    don't render as identical, unexplained duplicate buttons."""
    platform = link.get("platform", "")
    base = FUNDING_LABEL.get(platform, platform.replace("_", " ").title())
    if platform == "custom":
        return base  # an arbitrary donation page, not a per-person handle
    handle = link.get("url", "").rstrip("/").rsplit("/", 1)[-1]
    return f"{base} · {handle}" if handle else base


def install_options(app: App) -> list[dict[str, Any]]:
    """Per install source: what a person at a terminal would type."""
    out = []
    for s in sorted(app.sources, key=lambda s: s.priority):
        kind = SourceKind(s.kind)
        if kind is SourceKind.FLATHUB:
            out.append({"kind": "flathub", "title": "From Flathub",
                        "commands": [f"flatpak install flathub {app.app_id}"],
                        "note": "Built and reviewed by Flathub; updates come from Flathub."})
        elif kind is SourceKind.REMOTE and s.remote_url:
            name = s.remote_name or "project"
            out.append({"kind": "remote", "title": "From the project's own remote",
                        "commands": [f"flatpak remote-add --if-not-exists {name} {s.remote_url}",
                                     f"flatpak install {name} {app.app_id}"],
                        "link": s.remote_url,
                        "note": "Adds a third-party remote: future updates of this app come from it."})
        elif kind is SourceKind.BUNDLE and s.bundle_url:
            fname = s.bundle_url.rsplit("/", 1)[-1] or f"{app.app_id}.flatpak"
            out.append({"kind": "bundle", "title": "From a release bundle",
                        "commands": [f"flatpak install ./{fname}"], "link": s.bundle_url, "link_text": fname,
                        "note": "A single .flatpak file attached to a release. Bundles do not update themselves."})
        elif kind is SourceKind.MANIFEST and s.manifest_url:
            fname = s.manifest_url.rsplit("/", 1)[-1]
            out.append({"kind": "manifest", "title": "Build it from source",
                        "commands": [f"flatpak-builder --user --install --install-deps-from=flathub build {fname}"],
                        "link": s.manifest_url, "link_text": fname,
                        "note": "Only a flatpak-builder manifest exists; the build runs on your machine."})
    return out


def query_url(request: Request, **changes: Any) -> str:
    """The current path with some query parameters replaced (``page=2``) or dropped (``None``)."""
    params = {k: v for k, v in request.query_params.items() if v != ""}
    for k, v in changes.items():
        if v is None or v == "" or (k == "page" and v == 1):
            params.pop(k, None)
        else:
            params[k] = v
    return request.url.path + ("?" + urlencode(params) if params else "")


def base_url(request: Request) -> str:
    return (settings.site_url or str(request.base_url)).rstrip("/")


def _dt(d: datetime | None) -> str:
    return (d or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nicedate(d: datetime | None, fmt: str = "%b %d, %Y") -> str:
    return d.strftime(fmt).replace(" 0", " ") if d else ""


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["paragraphs"] = paragraphs
templates.env.filters["funding_label"] = funding_label
templates.env.filters["isodate"] = _dt
templates.env.filters["nicedate"] = _nicedate
templates.env.filters["browsable_categories"] = catalogue.browsable_categories
templates.env.globals.update(
    site=SITE, RISK_TEXT=RISK_TEXT, RISK_TIP=RISK_TIP, TRUST_TEXT=TRUST_TEXT, TRUST_TIP=TRUST_TIP,
    MAINTENANCE_TEXT=MAINTENANCE_TEXT, MAINTENANCE_TIP=MAINTENANCE_TIP,
    SORT_LABELS=SORT_LABELS, RISK_FILTERS=RISK_FILTERS, TRUST_FILTERS=TRUST_FILTERS,
    MAINTENANCE_FILTERS=MAINTENANCE_FILTERS, WHERE_FILTERS=WHERE_FILTERS,
    query_url=query_url, base_url=base_url,
    STATIC_BUILD=False,  # flipped to True for the duration of a GitHub Pages build; see web.build
)


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# --- pages -------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_session)):
    stats = catalogue.catalogue_stats(db)
    newest, _ = catalogue.page_apps(db, catalogue.apps_query(), "newest", 1, 8)
    loved, _ = catalogue.page_apps(db, catalogue.apps_query(funding_only=True), "stars", 1, 8)
    hunted, _ = catalogue.page_apps(db, catalogue.apps_query(on_flathub=False), "stars", 1, 8)
    return render(request, "index.html", stats=stats, newest=newest, loved=loved, hunted=hunted,
                  categories=catalogue.category_counts(db)[:12])


@router.get("/apps", response_class=HTMLResponse)
def apps(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    risk: str | None = Query(None, pattern="^(green|yellow|red|)$"),
    trust: str | None = None,
    maintenance: str | None = None,
    where: str | None = Query(None, pattern="^(flathub|hunted|)$"),
    sort: str = Query("name", pattern="^(name|stars|updated|newest)$"),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_session),
):
    on_flathub = {"flathub": True, "hunted": False}.get(where or "")
    try:
        stmt = catalogue.apps_query(q=q, category=category, risk=risk or None, trust=trust or None,
                                    maintenance=maintenance or None, on_flathub=on_flathub)
    except catalogue.BadFilter:
        trust = maintenance = None
        stmt = catalogue.apps_query(q=q, category=category, risk=risk or None, on_flathub=on_flathub)
    rows, total = catalogue.page_apps(db, stmt, sort, page, PER_PAGE, q=q)
    pages = max(1, -(-total // PER_PAGE))
    return render(request, "apps.html", apps=rows, total=total, page=page, pages=pages, q=q or "",
                  category=category or "", risk=risk or "", trust=trust or "", maintenance=maintenance or "",
                  where=where or "", sort=sort, categories=catalogue.category_counts(db))


@router.get("/apps/{app_id}", response_class=HTMLResponse)
def app_page(request: Request, app_id: str, db: Session = Depends(get_session)):
    app = catalogue.get_app(db, app_id)
    if app is None or not app.is_oss:
        return render(request, "404.html", status_code=404, what=f"No open-source app with the id {app_id}")
    findings = sorted(app.trust_findings or [], key=lambda f: {"red": 0, "yellow": 1, "green": 2}.get(f.get("level"), 1))
    perms = sorted(app.permissions or [], key=lambda p: {"red": 0, "yellow": 1, "green": 2}.get(p.get("level"), 1))
    return render(request, "app.html", app=app, findings=findings, perms=perms, options=install_options(app),
                  n_bad=sum(1 for p in perms if p.get("level") != "green"))


@router.get("/about", response_class=HTMLResponse)
def about(request: Request, db: Session = Depends(get_session)):
    return render(request, "about.html", stats=catalogue.catalogue_stats(db))


_admin_auth = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(_admin_auth)) -> None:
    """Gates /admin/analytics. Fails closed: no ADMIN_TOKEN configured means the
    route 404s, not that it's open. Username is ignored; the password is the token,
    compared with a timing-safe check."""
    if not settings.admin_token:
        raise HTTPException(404, "Not Found")
    valid = credentials is not None and secrets.compare_digest(credentials.password, settings.admin_token)
    if not valid:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


@router.get("/admin/analytics", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def analytics(request: Request, db: Session = Depends(get_session)):
    """Never linked from the nav, never built into the static GitHub Pages export
    (see web/build.py's page list). Requires ADMIN_TOKEN; see require_admin above."""
    return render(request, "analytics.html", stats=catalogue.catalogue_stats(db),
                  categories=catalogue.category_counts(db)[:10],
                  daily=catalogue.discoveries_by_day(db, 30),
                  runs=catalogue.crawl_history(db, 40))


def render_404(request: Request) -> HTMLResponse:
    return render(request, "404.html", status_code=404, what="That page does not exist")


# --- machine-readable ---------------------------------------------------------------------


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return (f"User-agent: *\nAllow: /\n"
            f"Disallow: /api/\nDisallow: /admin/\nDisallow: /docs\nDisallow: /redoc\nDisallow: /openapi.json\n"
            f"Sitemap: {base_url(request)}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap(request: Request, db: Session = Depends(get_session)):
    rows = db.execute(select(App.app_id, App.updated_at).where(App.is_oss.is_(True)).order_by(App.app_id)).all()
    xml = templates.get_template("sitemap.xml").render(request=request, rows=rows)
    return Response(xml, media_type="application/xml")


@router.get("/feed.xml")
def feed(request: Request, db: Session = Depends(get_session)):
    newest, _ = catalogue.page_apps(db, catalogue.apps_query(), "newest", 1, 50)
    xml = templates.get_template("feed.xml").render(request=request, apps=newest)
    return Response(xml, media_type="application/atom+xml")
