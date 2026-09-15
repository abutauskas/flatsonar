from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared connection, or every thread sees an empty DB
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_db(eng=None) -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(eng or engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with SessionLocal() as session:
        yield session
