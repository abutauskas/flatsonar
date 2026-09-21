"""Render the website to static files for GitHub Pages (or any static host).

    python -m flatsonar_server.web.build --out ../public --site-url https://abutauskas.github.io/flatsonar

Reuses the live app's exact templates, catalogue queries and static assets. The
dynamic ``uvicorn`` app is never touched and this never writes to the database.
The one thing that cannot carry over is server-side search/filter/sort on
``/apps``: GitHub Pages has no server to run that query against, so every
open-source app is rendered into the page once (see ``apps_static.html``) and
``site.js`` filters/sorts/paginates over that same markup in the browser.

``--site-url`` should be the *full* public URL including any path prefix a
project page is served under (``https://<user>.github.io/<repo>``); the prefix
is applied to every root-relative link in the rendered HTML (see
``_prefix_base_path``). A user/organization page or a custom domain at the
root needs no prefix and can pass a bare origin.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import catalogue
from ..db import SessionLocal, init_db
from ..models import App
from ..settings import settings
from . import routes as web_routes

log = logging.getLogger("flatsonar.web.build")

ALL = 1_000_000  # "no pagination": the static catalogue page carries every app
LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2}

# href="/..." / src="/..." / action="/..." (not "//", a protocol-relative URL).
# Absolute URLs built via base_url() already carry the site's full path and
# never match this (they start with "https:", not "/").
_LINK_RE = re.compile(r'(href|src|action)="/(?!/)')


@dataclass
class _URL:
    path: str


@dataclass
class FakeRequest:
    """Just enough of Starlette's ``Request`` for the templates to render: a
    logical, base-path-free path (``/apps``, never ``/flatsonar/apps``; the base
    path is applied afterward, uniformly, by :func:`_prefix_base_path`) and no
    query string. The static catalogue page has no server-side filters; its JS
    reads the real ``location.search`` in the visitor's browser instead."""

    url: _URL
    query_params: dict = field(default_factory=dict)

    @classmethod
    def at(cls, path: str) -> "FakeRequest":
        return cls(_URL(path))


def _prefix_base_path(html: str, base_path: str) -> str:
    if not base_path:
        return html
    return _LINK_RE.sub(lambda m: f'{m.group(1)}="{base_path}/', html)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sorted_findings(items: list[dict] | None) -> list[dict]:
    return sorted(items or [], key=lambda f: LEVEL_ORDER.get(f.get("level"), 1))


class Builder:
    def __init__(self, out: Path, site_url: str):
        self.out = out
        self.site_url = site_url.rstrip("/")
        self.base_path = urlsplit(self.site_url).path.rstrip("/")
        settings.site_url = self.site_url
        # Only ever set True inside this standalone process; the live uvicorn
        # app never runs in the same interpreter, so this can't leak into it.
        web_routes.templates.env.globals["STATIC_BUILD"] = True

    def render(self, name: str, path: str, dest_name: str | None = None, **ctx) -> None:
        """Render ``name`` for the logical ``path`` (what canonical/og:url and the
        nav's active-page state use) and write it, base-path prefixed. Pretty URLs
        by default (``/apps/<id>`` -> ``apps/<id>/index.html``); ``dest_name`` gives
        an exact top-level filename instead (``sitemap.xml``, ``404.html``, ...)."""
        html = web_routes.templates.get_template(name).render(request=FakeRequest.at(path), **ctx)
        html = _prefix_base_path(html, self.base_path)
        dest = self.out / dest_name if dest_name else self.out / path.strip("/") / "index.html"
        _write(dest, html)

    def build(self) -> None:
        init_db()
        with SessionLocal() as db:
            self._build_pages(db)
        self._copy_static()
        _write(self.out / ".nojekyll", "")  # tell GitHub Pages not to run Jekyll over this
        log.info("built static site at %s for %s", self.out, self.site_url)

    def _build_pages(self, db: Session) -> None:
        stats = catalogue.catalogue_stats(db)
        categories = catalogue.category_counts(db)
        newest, _ = catalogue.page_apps(db, catalogue.apps_query(), "newest", 1, 8)
        loved, _ = catalogue.page_apps(db, catalogue.apps_query(funding_only=True), "stars", 1, 8)
        hunted, _ = catalogue.page_apps(db, catalogue.apps_query(on_flathub=False), "stars", 1, 8)
        self.render("index.html", "/", stats=stats, newest=newest, loved=loved, hunted=hunted,
                   categories=categories[:12])

        # Eager-load once for all apps, not per app: _build_app_page used to call
        # catalogue.get_app() (a selectinload per app) inside the loop below, which
        # is the right shape for a single live request but turns into thousands of
        # extra round trips here. Fine against local SQLite; against a remote
        # Postgres it was the whole difference between a ~13s build and 15+ minutes.
        all_apps_stmt = catalogue.apps_query().options(selectinload(App.sources), selectinload(App.manifest))
        all_apps, total = catalogue.page_apps(db, all_apps_stmt, "name", 1, ALL)
        self.render("apps_static.html", "/apps", apps=all_apps, total=total, categories=categories)
        self.render("about.html", "/about", stats=stats)
        self.render("404.html", "/404", dest_name="404.html", what="That page does not exist")

        rows = db.execute(select(App.app_id, App.updated_at).where(App.is_oss.is_(True)).order_by(App.app_id)).all()
        self.render("sitemap.xml", "/sitemap.xml", dest_name="sitemap.xml", rows=rows)
        recent, _ = catalogue.page_apps(db, catalogue.apps_query(), "newest", 1, 50)
        self.render("feed.xml", "/feed.xml", dest_name="feed.xml", apps=recent)

        self._write_robots()
        self._write_apps_json(all_apps)
        for i, app in enumerate(all_apps):
            self._build_app_page(app)
            if (i + 1) % 200 == 0:
                log.info("rendered %d/%d app pages", i + 1, len(all_apps))

    def _build_app_page(self, app: App) -> None:
        full = app  # already eager-loaded with sources + manifest, see _build_pages
        manifest_href = "manifest.json" if full.manifest else None
        perms = _sorted_findings(full.permissions)
        self.render("app.html", f"/apps/{app.app_id}", app=full, findings=_sorted_findings(full.trust_findings),
                   perms=perms, options=web_routes.install_options(full), manifest_href=manifest_href,
                   n_bad=sum(1 for p in perms if p.get("level") != "green"))
        if full.manifest:
            m = full.manifest
            data = {
                "app_id": app.app_id, "source_url": m.source_url, "runtime": m.runtime,
                "runtime_version": m.runtime_version, "sdk": m.sdk, "finish_args": m.finish_args,
                "module_count": m.module_count, "fetched_at": m.fetched_at.isoformat() if m.fetched_at else None,
                "manifest": m.raw,
            }
            _write(self.out / "apps" / app.app_id / "manifest.json", json.dumps(data, indent=1))

    def _write_robots(self) -> None:
        base = self.base_path or ""
        _write(self.out / "robots.txt",
              f"User-agent: *\nAllow: {base or '/'}\nDisallow: {base}/api/\nDisallow: {base}/admin/\n"
              f"Sitemap: {self.site_url}/sitemap.xml\n")

    def _write_apps_json(self, apps: list[App]) -> None:
        """A static stand-in for the JSON API GitHub Pages can't run: every field
        the catalogue summary carries, one file, fetchable once and cached."""
        dump = [{
            "app_id": a.app_id, "name": a.name, "summary": a.summary, "icon_url": a.icon_url,
            "categories": a.categories, "license": a.license, "developer_name": a.developer_name,
            "risk_level": a.risk_level, "on_flathub": a.on_flathub, "flathub_verified": a.flathub_verified,
            "trust": a.trust, "stars": a.stars, "latest_version": a.latest_version,
            "has_funding": bool(a.funding_links),
        } for a in apps]
        _write(self.out / "apps.json", json.dumps(dump, indent=1))

    def _copy_static(self) -> None:
        dest = self.out / "static"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(web_routes.STATIC_DIR, dest)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the Flatsonar website to static files for GitHub Pages.")
    p.add_argument("--out", required=True, help="output directory (wiped and rebuilt each run)")
    p.add_argument("--site-url", required=True,
                   help="public URL the site is served at, e.g. https://abutauskas.github.io/flatsonar")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    Builder(Path(args.out), args.site_url).build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
