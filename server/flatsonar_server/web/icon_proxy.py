"""Serve app icons from our own domain instead of hotlinking Flathub/GitHub/GitLab/
Codeberg CDNs directly in every visitor's browser: that leaks each visitor's IP and
referrer to those hosts on every page view, and breaks the moment one of them
rate-limits, moves a file, or goes down. Fetch once, cache to disk, serve from here.

Misses (no icon_url, or the fetch failed) get a ``.missing`` marker so we don't retry
a known-bad URL on every subsequent page view.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import catalogue
from ..db import get_session
from ..settings import settings

log = logging.getLogger("flatsonar.icons")
router = APIRouter(include_in_schema=False)

CACHE_DIR = settings.icon_cache_dir
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FALLBACK = "/static/app-fallback.svg"
CACHE_HEADERS = {"Cache-Control": "public, max-age=604800"}  # a week; re-crawls change icon_url, not the file
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _stem(app_id: str) -> str:
    return _UNSAFE.sub("_", app_id)


def _cached(app_id: str) -> Path | None:
    return next(CACHE_DIR.glob(f"{_stem(app_id)}.*"), None)


def _miss(app_id: str) -> RedirectResponse:
    (CACHE_DIR / f"{_stem(app_id)}.missing").touch(exist_ok=True)
    return RedirectResponse(FALLBACK)


@router.get("/icon/{app_id}")
def get_icon(app_id: str, db: Session = Depends(get_session)):
    cached = _cached(app_id)
    if cached is not None:
        if cached.suffix == ".missing":
            return RedirectResponse(FALLBACK)
        return FileResponse(cached, headers=CACHE_HEADERS)

    app = catalogue.get_app(db, app_id)
    if app is None or not app.icon_url:
        return _miss(app_id)

    try:
        resp = httpx.get(app.icon_url, headers={"User-Agent": settings.user_agent},
                          timeout=10.0, follow_redirects=True)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code != 200 or not ctype.startswith("image/"):
            raise ValueError(f"status {resp.status_code}, content-type {ctype!r}")
    except (httpx.HTTPError, ValueError) as exc:
        log.info("icon fetch failed for %s (%s): %s", app_id, app.icon_url, exc)
        return _miss(app_id)

    ext = {"image/svg+xml": ".svg", "image/jpeg": ".jpg"}.get(ctype) or mimetypes.guess_extension(ctype) or ".img"
    path = CACHE_DIR / f"{_stem(app_id)}{ext}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(path)  # atomic: concurrent first-requests for the same icon don't corrupt it
    return FileResponse(path, headers=CACHE_HEADERS)
