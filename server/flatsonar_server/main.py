from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from .api.apps import router as apps_router
from .db import init_db
from .settings import settings
from .web.routes import STATIC_DIR, render_404
from .web.routes import router as web_router

_MACHINE_PREFIXES = ("/api", "/static", "/docs", "/redoc", "/openapi.json")
# /health too: Render's own health checks hit the app on its internal onrender.com
# address, never on the custom domain, so that path must never be redirected either.
_NO_HOST_REDIRECT = _MACHINE_PREFIXES + ("/health",)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


# Inline <style width="...%"> bars (analytics/app pages) and the onerror icon
# fallback keep 'unsafe-inline' necessary here; everything else is locked to same
# origin. Still blocks framing, plugins and any externally-hosted script/style.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


_site = urlsplit(settings.site_url) if settings.site_url else None
_CANONICAL_NETLOC = _site.netloc if _site else None
_CANONICAL_SCHEME = _site.scheme if _site else "https"


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """301s every human-facing page to the SITE_URL host (flatsonar.org) so the same
    content never gets indexed twice under a second hostname (e.g. the Render-assigned
    *.onrender.com one the custom domain sits in front of). A no-op when SITE_URL isn't
    configured. Never redirects /api (the desktop client talks to onrender.com directly,
    see client/flatsonar/api.py's DEFAULT_API) or /health (see _NO_HOST_REDIRECT)."""

    async def dispatch(self, request: Request, call_next):
        if (_CANONICAL_NETLOC and request.url.netloc != _CANONICAL_NETLOC
                and not request.url.path.startswith(_NO_HOST_REDIRECT)):
            target = request.url.replace(scheme=_CANONICAL_SCHEME, netloc=_CANONICAL_NETLOC)
            return RedirectResponse(str(target), status_code=301)
        return await call_next(request)


app = FastAPI(
    title="Flatsonar",
    description="Open-source Flatpak apps, hunted from everywhere. Credits creators, flags risky sandboxes.",
    version="1.0.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CanonicalHostMiddleware)
app.include_router(apps_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(StarletteHTTPException)
async def _not_found_page(request: Request, exc: StarletteHTTPException):
    """Humans get the 404 page; the API and its docs keep their JSON errors."""
    if exc.status_code == 404 and not request.url.path.startswith(_MACHINE_PREFIXES):
        return render_404(request)
    return await http_exception_handler(request, exc)


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
