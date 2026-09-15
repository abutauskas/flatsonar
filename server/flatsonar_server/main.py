from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.apps import router as apps_router
from .db import init_db
from .settings import settings


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Flatsonar",
    description="Open-source Flatpak apps, hunted from everywhere. Credits creators, flags risky sandboxes.",
    version="0.1.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(apps_router)


@app.get("/", include_in_schema=False)
def root():
    return {"name": "flatsonar", "docs": "/docs", "api": "/api/apps"}
