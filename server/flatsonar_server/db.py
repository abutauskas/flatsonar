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
    elif url.startswith("postgresql"):
        # A transaction-mode pooler (Supabase's Supavisor on :6543, PgBouncer in the
        # same mode elsewhere) can hand a different backend connection to each
        # transaction. psycopg3's default automatic server-side PREPARE (after a
        # statement repeats a few times) then collides with or vanishes from
        # whatever backend the pooler happens to route to next -
        # DuplicatePreparedStatement / InvalidSqlStatementName / ProtocolViolation,
        # and once one of those corrupts a connection mid-session, unrelated-looking
        # errors follow as knock-on damage. Disabling server-side prepare is
        # Supabase's own documented fix for exactly this pooler mode, and is safe
        # (if slightly less efficient) against a session-mode pooler too.
        kwargs["connect_args"] = {"prepare_threshold": None}
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
# autoflush (the default) matters here: upsert_candidate's db.get(App, app_id) has to
# see a same-app-id row added earlier in the *same* uncommitted batch, or two crawl
# candidates for one id (e.g. the same app found on two different GitLab instances,
# neither committed yet) both look "new" and collide on the primary key at commit time
# instead of going through the existing collision handling in _guard_collision.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db(eng=None) -> None:
    from . import models  # noqa: F401  (register tables)

    eng = eng or engine
    Base.metadata.create_all(eng)
    _add_missing_columns(eng)


def _add_missing_columns(eng) -> None:
    """Poor man's migration: columns added to a model after the DB was created get
    ``ALTER TABLE ... ADD COLUMN``-ed with their default. Enough for SQLite-on-a-laptop;
    a real deployment should run Alembic."""
    from sqlalchemy import JSON, inspect, text

    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(eng.dialect)}"
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(col.type, JSON):
                    ddl += " DEFAULT '[]'"  # every JSON column here is a list
                elif isinstance(default, bool):
                    # Not str(default) or int(default): Postgres accepts the TRUE/FALSE
                    # keywords for a BOOLEAN column but not "True"/"False" or a bare 0/1
                    # (DatatypeMismatch, integer default on a boolean column). SQLite
                    # (3.23+) accepts TRUE/FALSE as 1/0 too, so this works on both.
                    ddl += f" DEFAULT {'TRUE' if default else 'FALSE'}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += f" DEFAULT '{default}'"
                conn.execute(text(ddl))


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with SessionLocal() as session:
        yield session
