"""The static site generator (web/build.py): every internal link gets the site's
base path, machine-readable files are correct, and the output actually matches
what the live dynamic pages would show for the same catalogue."""

import json

import pytest

from flatsonar_server import db as dbmod
from flatsonar_server.crawler.base import upsert_candidate
from flatsonar_server.web import build as build_module
from flatsonar_server.web.build import Builder

from test_server import _flathub_candidate


@pytest.fixture(autouse=True)
def _route_build_through_the_test_db(session, monkeypatch):
    """``Builder.build()`` opens its own sessions via ``SessionLocal()`` (it has to:
    it is a standalone CLI, not a request handler with DI to lean on) using the name
    ``web/build.py`` imported at module load. Point that name at the same in-memory
    db the ``session`` fixture already wired up, so seeding through ``session`` and
    building through ``Builder`` see the same data."""
    monkeypatch.setattr(build_module, "SessionLocal", dbmod.SessionLocal)


def _seed(session):
    from flatsonar_core import TrustLevel

    upsert_candidate(session, _flathub_candidate(trust=TrustLevel.VERIFIED))
    upsert_candidate(session, _flathub_candidate("com.spotify.Client", name="Spotify",
                                                 license="LicenseRef-proprietary", is_oss=False))
    session.commit()


def test_build_writes_every_page_with_the_base_path_applied(tmp_path, session):
    _seed(session)
    out = tmp_path / "public"
    Builder(out, "https://abutauskas.github.io/flatsonar").build()

    assert (out / "index.html").exists()
    assert (out / "apps" / "index.html").exists()
    assert (out / "apps" / "org.gnome.Calculator" / "index.html").exists()
    assert (out / "about" / "index.html").exists()
    assert (out / "404.html").exists()
    assert (out / "sitemap.xml").exists()
    assert (out / "feed.xml").exists()
    assert (out / "robots.txt").exists()
    assert (out / "apps.json").exists()
    assert (out / "static" / "style.css").exists()
    assert (out / ".nojekyll").exists()
    assert (out / "apps" / "org.gnome.Calculator" / "manifest.json").exists()

    home = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/flatsonar/apps"' in home
    assert 'href="/flatsonar/about#get"' in home
    assert 'src="/flatsonar/static/logo.svg#mark"' not in home  # it's <use href=...>, not <img src=...>
    assert 'href="/flatsonar/static/logo.svg#mark"' in home
    assert "Calculator" in home
    assert "Spotify" not in home  # proprietary: never listed anywhere

    app_page = (out / "apps" / "org.gnome.Calculator" / "index.html").read_text(encoding="utf-8")
    assert 'href="/flatsonar/apps?category=Utility"' in app_page
    assert "https://abutauskas.github.io/flatsonar/apps/org.gnome.Calculator" in app_page  # canonical/og:url
    assert 'href="https://flathub.org/apps/org.gnome.Calculator"' in app_page  # external: untouched
    assert "Catalogue JSON" in app_page and "Manifest (JSON)" in app_page
    assert 'href="manifest.json"' in app_page  # relative: not touched by base-path prefixing, no need to be


def test_apps_static_page_carries_every_app_and_its_filter_data(tmp_path, session):
    _seed(session)
    out = tmp_path / "public"
    Builder(out, "https://abutauskas.github.io/flatsonar").build()
    apps_page = (out / "apps" / "index.html").read_text(encoding="utf-8")

    assert apps_page.count('class="app-card"') == 1  # Calculator only; Spotify is proprietary
    assert 'data-risk="green"' in apps_page
    assert 'data-trust="verified"' in apps_page
    assert 'data-where="flathub"' in apps_page
    assert 'data-category="Utility"' in apps_page
    assert 'href="?category=Utility"' in apps_page  # relative: no base-path prefix needed or applied
    assert "1 apps" in apps_page or "1 app" in apps_page.replace("1 apps", "1 app")


def test_robots_sitemap_feed_and_apps_json_use_the_full_site_url(tmp_path, session):
    _seed(session)
    out = tmp_path / "public"
    Builder(out, "https://abutauskas.github.io/flatsonar/").build()  # trailing slash: must be stripped cleanly

    robots = (out / "robots.txt").read_text(encoding="utf-8")
    assert "Allow: /flatsonar" in robots
    assert "Disallow: /flatsonar/api/" in robots
    assert "Sitemap: https://abutauskas.github.io/flatsonar/sitemap.xml" in robots

    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://abutauskas.github.io/flatsonar/apps/org.gnome.Calculator</loc>" in sitemap
    assert "spotify" not in sitemap.lower()

    feed = (out / "feed.xml").read_text(encoding="utf-8")
    assert "<title>Calculator</title>" in feed
    assert "https://abutauskas.github.io/flatsonar/apps/org.gnome.Calculator" in feed

    dump = json.loads((out / "apps.json").read_text(encoding="utf-8"))
    assert [a["app_id"] for a in dump] == ["org.gnome.Calculator"]
    assert dump[0]["has_funding"] is False and dump[0]["trust"] == "verified"


def test_manifest_json_is_written_for_apps_that_have_one(tmp_path, session):
    from flatsonar_core import parse_manifest_text

    from test_server import MANIFEST

    m = parse_manifest_text(MANIFEST)
    cand = _flathub_candidate("com.example.Hunted", manifest=m, is_oss=True)
    upsert_candidate(session, cand)
    session.commit()

    out = tmp_path / "public"
    Builder(out, "https://abutauskas.github.io/flatsonar").build()

    manifest_json = out / "apps" / "com.example.Hunted" / "manifest.json"
    assert manifest_json.exists()
    data = json.loads(manifest_json.read_text(encoding="utf-8"))
    assert data["app_id"] == "com.example.Hunted"
    assert data["finish_args"] == ["--socket=wayland", "--filesystem=host"]

    app_page = (out / "apps" / "com.example.Hunted" / "index.html").read_text(encoding="utf-8")
    assert 'href="manifest.json"' in app_page  # relative: resolves under this app's own directory


def test_no_apps_still_produces_a_valid_shell(tmp_path, session):
    out = tmp_path / "public"
    Builder(out, "https://abutauskas.github.io/flatsonar").build()
    assert (out / "index.html").exists()
    assert (out / "apps" / "index.html").exists()
    assert '0 apps' in (out / "apps" / "index.html").read_text(encoding="utf-8")


def test_root_site_url_needs_no_base_path(tmp_path, session):
    """A custom domain or a user/organization page (served at the root) should not
    get a spurious prefix on every internal link."""
    _seed(session)
    out = tmp_path / "public"
    Builder(out, "https://flatsonar.example").build()
    home = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/apps"' in home
    assert 'href="/flatsonar' not in home
