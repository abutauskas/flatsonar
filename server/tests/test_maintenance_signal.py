"""The maintenance signal end to end: assessment, the upsert, the API filter and stats,
and the add-column migration - mirrors test_trust.py's structure for the same reasons."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine, inspect, text

from flatsonar_core import MaintenanceLevel, parse_manifest_text
from flatsonar_server.crawler.base import Candidate, Fetched, SourceSpec, upsert_candidate
from flatsonar_server.crawler.forge import RepoInfo
from flatsonar_server.crawler.trust import assess
from flatsonar_server.models import SourceKind

MANIFEST = """
app-id: {app_id}
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: foo
finish-args: [--socket=wayland]
modules:
  - name: foo
    sources:
      - type: git
        url: {url}
        tag: v1
"""


def _repo(html_url="https://github.com/alice/foo", pushed_days_ago=10, archived=False, **kw) -> RepoInfo:
    parts = html_url.split("/")
    now = datetime.now(timezone.utc)
    base = dict(
        forge="github", full_name=f"{parts[3]}/{parts[4]}", html_url=html_url, owner=parts[3], default_branch="main",
        created_at=now - timedelta(days=900), pushed_at=now - timedelta(days=pushed_days_ago), archived=archived,
    )
    base.update(kw)
    return RepoInfo(**base)


def _cand(app_id="io.github.alice.Foo", src="https://github.com/alice/foo.git", **kw) -> Candidate:
    m = parse_manifest_text(MANIFEST.format(app_id=app_id, url=src), f"{app_id}.yml")
    base = dict(app_id=app_id, name="Foo", license="MIT", is_oss=True, manifest=m,
                upstream_url="https://github.com/alice/foo",
                sources=[SourceSpec(kind=SourceKind.MANIFEST, manifest_url="https://raw/x.yml")])
    base.update(kw)
    return Candidate(**base)


class FakeCtx:
    """assess() only touches ctx.fetch for the well-known-file check, which none of
    these candidates trigger (forge namespaces never hit the network)."""

    async def fetch(self, url, headers=None, use_cache=True):
        return Fetched(404, "", httpx.Headers())


def _assess(cand, repo=None):
    return asyncio.run(assess(FakeCtx(), cand, repo))


# --- assess ---------------------------------------------------------------------------


def test_flathub_candidate_is_always_active():
    c = _assess(Candidate(app_id="org.x.A", on_flathub=True))
    assert c.maintenance is MaintenanceLevel.ACTIVE and c.maintenance_findings == []


def test_recently_pushed_repo_is_active():
    c = _assess(_cand(), _repo(pushed_days_ago=5))
    assert c.maintenance is MaintenanceLevel.ACTIVE


def test_quiet_repo_is_stale():
    c = _assess(_cand(), _repo(pushed_days_ago=800))
    assert c.maintenance is MaintenanceLevel.STALE
    assert c.maintenance_findings[0].arg == "maintenance:stale"


def test_archived_repo_is_abandoned_even_if_recently_pushed():
    c = _assess(_cand(), _repo(pushed_days_ago=1, archived=True))
    assert c.maintenance is MaintenanceLevel.ABANDONED
    assert c.maintenance_findings[0].arg == "maintenance:archived"


# --- upsert -----------------------------------------------------------------------------


def test_upsert_persists_maintenance(session):
    app, _ = upsert_candidate(session, _assess(_cand(), _repo(pushed_days_ago=2000)))
    session.commit()
    assert app.maintenance == "abandoned"
    assert app.maintenance_findings[0]["check"] == "maintenance:stale"


def test_flathub_app_stays_active_even_when_enriched_by_an_archived_upstream(session):
    fh = Candidate(app_id="org.gnome.Calculator", name="Calculator", is_oss=True, on_flathub=True,
                   upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator",
                   sources=[SourceSpec(kind=SourceKind.FLATHUB, remote_name="flathub")])
    app, _ = upsert_candidate(session, _assess(fh))
    session.commit()
    assert app.maintenance == "active"

    # A forge crawl later finds the (now-archived) upstream repo: Flathub's row is
    # authoritative and stays "active" - see upsert_candidate's enrichment branch.
    enrich = _cand("org.gnome.Calculator", src="https://gitlab.gnome.org/GNOME/gnome-calculator.git",
                   upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator", on_flathub=False, archived=True)
    app, created = upsert_candidate(session, _assess(enrich, _repo("https://gitlab.gnome.org/GNOME/gnome-calculator",
                                                                   archived=True)))
    session.commit()
    assert not created and app.maintenance == "active" and app.archived is True


# --- api ----------------------------------------------------------------------------------


def test_api_maintenance_filter_and_stats(client, session):
    upsert_candidate(session, _assess(_cand(), _repo(pushed_days_ago=5)))
    upsert_candidate(session, _assess(_cand("com.example.Foo"), _repo(pushed_days_ago=2000)))
    upsert_candidate(session, _assess(_cand("com.example.Bar", src="https://github.com/alice/bar.git",
                                            upstream_url="https://github.com/alice/bar"),
                                      _repo("https://github.com/alice/bar", pushed_days_ago=800)))
    session.commit()

    assert client.get("/api/apps").json()["total"] == 3
    assert client.get("/api/apps?maintenance=active").json()["total"] == 1
    assert client.get("/api/apps?maintenance=stale,abandoned").json()["total"] == 2
    assert client.get("/api/apps?maintenance=bogus").status_code == 422

    d = client.get("/api/apps/com.example.Foo").json()
    assert d["maintenance"] == "abandoned"
    assert d["maintenance_findings"][0]["check"] == "maintenance:stale"

    stats = client.get("/api/stats").json()
    assert stats["by_maintenance"] == {"active": 1, "stale": 1, "abandoned": 1}


# --- migration ------------------------------------------------------------------------


def test_init_db_adds_maintenance_columns(tmp_path):
    from flatsonar_server.db import init_db

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:  # a pre-maintenance schema with one row
        conn.execute(text("CREATE TABLE apps (app_id VARCHAR(255) PRIMARY KEY, name VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO apps (app_id, name) VALUES ('org.x.Old', 'Old')"))
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("apps")}
    assert {"maintenance", "maintenance_findings"} <= cols
    with engine.connect() as conn:
        maintenance, findings = conn.execute(text("SELECT maintenance, maintenance_findings FROM apps")).one()
    assert (maintenance, findings) == ("active", "[]")
