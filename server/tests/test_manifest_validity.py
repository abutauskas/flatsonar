"""Manifest validity: a listing whose manifest used to parse and no longer does gets
flagged rather than silently kept (or silently dropped) - covers candidates_from_repo's
detection, upsert_candidate's guarded application, and the add-column migration."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine, inspect, text

from flatsonar_core import parse_manifest_text
from flatsonar_server.crawler.base import Candidate, Fetched, SourceSpec, upsert_candidate
from flatsonar_server.crawler.forge import RepoInfo, candidates_from_repo
from flatsonar_server.crawler.trust import assess
from flatsonar_server.models import App, SourceKind

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

# Looks like a manifest (has "app-id", "runtime", "modules" substrings for the
# crawler's cheap pre-filter) but has no actual app-id key, so parsing fails.
BROKEN_MANIFEST = """
# app-id used to be here
runtime: org.gnome.Platform
modules: []
"""


class FakeForge:
    name = "fake"

    def raw_url(self, repo, path):
        return path

    async def load_tree(self, ctx, repo):
        pass

    async def load_releases(self, ctx, repo):
        pass


class FakeCtx:
    def __init__(self, files: dict[str, str] | None = None):
        self.files = files or {}

    async def fetch(self, url, headers=None, use_cache=True):
        if url in self.files:
            return Fetched(200, self.files[url], httpx.Headers())
        return Fetched(404, "", httpx.Headers())


def _repo(html_url="https://github.com/alice/foo", tree=None, **kw) -> RepoInfo:
    parts = html_url.split("/")
    base = dict(
        forge="github", full_name=f"{parts[3]}/{parts[4]}", html_url=html_url, owner=parts[3], default_branch="main",
        created_at=datetime.now(timezone.utc) - timedelta(days=900), tree=tree or [],
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


def _write(session, cand, repo=None):
    app, created = upsert_candidate(session, asyncio.run(assess(FakeCtx(), cand, repo)))
    session.commit()
    return app, created


# --- candidates_from_repo: detecting the failure -----------------------------------------


def test_broken_manifest_yields_a_manifest_error_candidate():
    repo = _repo(tree=["io.github.alice.Foo.yml"])
    cands = asyncio.run(candidates_from_repo(
        FakeCtx({"io.github.alice.Foo.yml": BROKEN_MANIFEST}), FakeForge(), repo))
    assert len(cands) == 1
    assert cands[0].app_id == "io.github.alice.Foo"
    assert cands[0].manifest is None and cands[0].name is None
    assert "no longer parses" in cands[0].manifest_error
    assert cands[0].upstream_url == "https://github.com/alice/foo"


def test_devel_suffix_is_stripped_from_the_derived_id():
    repo = _repo(tree=["io.github.alice.Foo.Devel.yml"])
    cands = asyncio.run(candidates_from_repo(
        FakeCtx({"io.github.alice.Foo.Devel.yml": BROKEN_MANIFEST}), FakeForge(), repo))
    assert cands[0].app_id == "io.github.alice.Foo"


def test_a_manifest_that_still_parses_is_unaffected():
    repo = _repo(tree=["io.github.alice.Foo.yml"], license_spdx="MIT")
    text_ = MANIFEST.format(app_id="io.github.alice.Foo", url="https://github.com/alice/foo.git")
    cands = asyncio.run(candidates_from_repo(FakeCtx({"io.github.alice.Foo.yml": text_}), FakeForge(), repo))
    assert len(cands) == 1
    assert cands[0].manifest_error is None and cands[0].manifest is not None


# --- upsert_candidate: applying it safely --------------------------------------------------


def test_manifest_error_on_a_never_indexed_app_is_a_noop(session):
    app, created = upsert_candidate(session, Candidate(
        app_id="io.github.nobody.Ghost", upstream_url="https://github.com/nobody/ghost",
        manifest_error="manifest no longer parses: manifest has no app-id"))
    session.commit()
    assert app is None and created is False
    assert session.get(App, "io.github.nobody.Ghost") is None


def test_manifest_error_from_an_unrelated_repo_does_not_mark_the_real_app_broken(session):
    app, _ = _write(session, _cand(), _repo())
    assert app.manifest_ok is True

    upsert_candidate(session, Candidate(
        app_id="io.github.alice.Foo", upstream_url="https://github.com/mallory/copycat",
        manifest_error="manifest no longer parses: manifest has no app-id"))
    session.commit()
    app = session.get(App, "io.github.alice.Foo")
    assert app.manifest_ok is True  # untouched: not the same repository


def test_manifest_error_marks_an_existing_app_from_the_same_repo(session):
    app, _ = _write(session, _cand(), _repo())
    assert app.manifest_ok is True and app.manifest_error is None

    upsert_candidate(session, Candidate(
        app_id="io.github.alice.Foo", upstream_url="https://github.com/alice/foo",
        manifest_error="manifest no longer parses: manifest has no app-id"))
    session.commit()
    app = session.get(App, "io.github.alice.Foo")
    assert app.manifest_ok is False
    assert "no app-id" in app.manifest_error
    # Everything else from the last good crawl stays exactly as it was.
    assert app.risk_level == "green" and app.name == "Foo"


def test_a_fixed_manifest_clears_the_flag(session):
    _write(session, _cand(), _repo())
    upsert_candidate(session, Candidate(app_id="io.github.alice.Foo", upstream_url="https://github.com/alice/foo",
                                        manifest_error="manifest no longer parses: manifest has no app-id"))
    session.commit()

    app, created = _write(session, _cand(), _repo())
    assert not created and app.manifest_ok is True and app.manifest_error is None


# --- api / migration -----------------------------------------------------------------------


def test_api_exposes_manifest_ok(client, session):
    _write(session, _cand(), _repo())
    upsert_candidate(session, Candidate(app_id="io.github.alice.Foo", upstream_url="https://github.com/alice/foo",
                                        manifest_error="manifest no longer parses: manifest has no app-id"))
    session.commit()

    summary = client.get("/api/apps").json()["items"][0]
    assert summary["manifest_ok"] is False
    detail = client.get("/api/apps/io.github.alice.Foo").json()
    assert detail["manifest_ok"] is False and "no app-id" in detail["manifest_error"]


def test_init_db_adds_manifest_validity_columns(tmp_path):
    from flatsonar_server.db import init_db

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE apps (app_id VARCHAR(255) PRIMARY KEY, name VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO apps (app_id, name) VALUES ('org.x.Old', 'Old')"))
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("apps")}
    assert {"manifest_ok", "manifest_error"} <= cols
    with engine.connect() as conn:
        (ok,) = conn.execute(text("SELECT manifest_ok FROM apps")).one()
    assert ok in (1, True)


# --- unfilled release templates --------------------------------------------------------------

# Shaped like Termix's packaging/flatpak/com.karmaa.termix.yml: the release workflow fills
# these in; the copy committed to the repository never builds.
TEMPLATE_MANIFEST = """
app-id: io.github.alice.Foo
runtime: org.freedesktop.Platform
runtime-version: '24.08'
sdk: org.freedesktop.Sdk
command: foo
finish-args: [--socket=wayland]
modules:
  - name: foo
    buildsystem: simple
    build-commands: ['install -Dm755 foo.AppImage ${FLATPAK_DEST}/bin/foo']
    sources:
      - type: file
        url: https://github.com/alice/foo/releases/download/release-VERSION_PLACEHOLDER-tag/foo_x64.AppImage
        sha256: CHECKSUM_X64_PLACEHOLDER
        dest-filename: foo.AppImage
"""
TEMPLATE_METAINFO = """<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.alice.Foo</id>
  <name>@APP_NAME@</name>
  <summary>Does foo things</summary>
  <project_license>MIT</project_license>
  <releases><release version="VERSION_PLACEHOLDER" date="2026-01-01"/></releases>
</component>
"""
BUNDLE = ("foo.flatpak", "https://github.com/alice/foo/releases/download/release-2.8.0-tag/foo.flatpak")


def _template_cands(files=None, **repo_kw):
    files = files or {"io.github.alice.Foo.yml": TEMPLATE_MANIFEST}
    repo = _repo(tree=list(files), license_spdx="MIT", **repo_kw)
    return asyncio.run(candidates_from_repo(FakeCtx(files), FakeForge(), repo))


def test_template_manifest_is_never_offered_as_a_build(client, session):
    (cand,) = _template_cands(release_assets=[BUNDLE])
    assert [s.kind for s in cand.sources] == [SourceKind.BUNDLE]  # the real release still installs
    assert "VERSION_PLACEHOLDER in a source URL" in cand.manifest_problem
    assert "'CHECKSUM_X64_PLACEHOLDER' is not a valid sha256" in cand.manifest_problem

    upsert_candidate(session, cand)
    session.commit()
    app = session.get(App, "io.github.alice.Foo")
    assert app.manifest_ok is False and "can't be built as committed" in app.manifest_error
    assert [s.kind for s in app.sources] == [SourceKind.BUNDLE]
    assert app.risk_level  # permissions are still scored from the template's finish-args

    page = client.get("/apps/io.github.alice.Foo").text
    assert "Build may be broken" in page and "VERSION_PLACEHOLDER in a source URL" in page
    assert "flatpak-builder --user --install" not in page  # no build command to copy


def test_template_only_repo_is_not_listed_and_an_old_listing_loses_its_build(session):
    (cand,) = _template_cands()
    assert cand.manifest is None and cand.drop_sources == [SourceKind.MANIFEST]
    assert upsert_candidate(session, cand) == (None, False)
    session.commit()
    assert session.get(App, "io.github.alice.Foo") is None

    # Listed by an earlier crawl, with the template as its install source.
    _write(session, _cand(), _repo())
    upsert_candidate(session, cand)
    session.commit()
    app = session.get(App, "io.github.alice.Foo")
    assert app.manifest_ok is False and "CHECKSUM_X64_PLACEHOLDER" in app.manifest_error
    assert app.sources == []


def test_template_metainfo_values_fall_back_instead_of_being_shown():
    (cand,) = _template_cands({"io.github.alice.Foo.yml": TEMPLATE_MANIFEST,
                               "io.github.alice.Foo.metainfo.xml": TEMPLATE_METAINFO}, release_assets=[BUNDLE])
    assert cand.name == "foo"  # the repository's name, not "@APP_NAME@"
    assert cand.summary == "Does foo things"
    assert cand.latest_version is None


def test_template_values_stored_by_an_older_crawl_are_cleared(session):
    _write(session, _cand(latest_version="VERSION_PLACEHOLDER", summary="@TAGLINE@"), _repo())
    app, _ = _write(session, _cand(), _repo())
    assert app.latest_version is None and app.summary == ""
