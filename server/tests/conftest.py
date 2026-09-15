import pytest
from sqlalchemy.orm import sessionmaker

from flatsea_server import db as dbmod
from flatsea_server.db import Base, make_engine


@pytest.fixture()
def session(monkeypatch):
    """Fresh in-memory SQLite for every test, wired into the app's SessionLocal."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", factory)
    monkeypatch.setattr(dbmod, "engine", engine)
    with factory() as s:
        yield s


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient

    from flatsea_server.db import get_session
    from flatsea_server.main import app

    def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    # Skip the lifespan (it would create tables on the real engine).
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
