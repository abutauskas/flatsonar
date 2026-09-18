"""The website: every page renders over the same catalogue the API serves."""

from flatsonar_core import TrustLevel

from flatsonar_server.crawler.base import SourceSpec, upsert_candidate
from flatsonar_server.models import SourceKind
from flatsonar_server.web.routes import install_options, paragraphs

from test_server import _flathub_candidate


def _seed(session):
    upsert_candidate(session, _flathub_candidate(funding_links=[{"platform": "liberapay", "url": "https://liberapay.com/gnome"}],
                                                  screenshots=["https://x/shot.png"], trust=TrustLevel.VERIFIED,
                                                  description="First paragraph.\n\n- one\n- two\n\nLast <b>bold</b>."))
    upsert_candidate(session, _flathub_candidate("com.spotify.Client", name="Spotify",
                                                 license="LicenseRef-proprietary", is_oss=False))
    hunted = _flathub_candidate("io.github.alice.Hunted", name="Hunted", on_flathub=False, flathub_verified=False,
                                upstream_url="https://github.com/alice/hunted", developer_name="alice",
                                summary="Hunts things", trust=TrustLevel.UNVERIFIED,
                                sources=[SourceSpec(kind=SourceKind.BUNDLE, bundle_url="https://github.com/alice/hunted/releases/download/v1/hunted.flatpak"),
                                         SourceSpec(kind=SourceKind.MANIFEST, manifest_url="https://raw.githubusercontent.com/alice/hunted/main/io.github.alice.Hunted.yml")])
    upsert_candidate(session, hunted)
    session.commit()


def test_home_renders_stats_and_sections(client, session):
    _seed(session)
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert "One store, every open-source Flatpak app." in html
    assert "Found outside Flathub" in html and "io.github.alice.Hunted" in html
    assert "Creators you can support" in html and "org.gnome.Calculator" in html
    assert "Spotify" not in html  # proprietary never shows


def test_catalogue_filters_and_pagination(client, session):
    _seed(session)
    assert "Calculator" in client.get("/apps").text
    assert "2 apps" in client.get("/apps").text
    page = " ".join(client.get("/apps?q=sums").text.split())
    assert "1 app matching" in page and "Hunted" not in page and "Calculator" in page
    assert "Hunted" in client.get("/apps?q=alice").text  # developer name is searched too
    assert "No apps found" in client.get("/apps?q=zzz").text
    assert "Hunted" in client.get("/apps?where=hunted").text and "Calculator" not in client.get("/apps?where=hunted").text
    assert "Calculator" in client.get("/apps?category=Utility").text
    assert client.get("/apps?trust=bogus").status_code == 200  # unknown trust is ignored, not a 500
    assert client.get("/apps?risk=purple").status_code == 422
    assert client.get("/apps?page=0").status_code == 422


def test_app_page_shows_install_options_permissions_and_funding(client, session):
    _seed(session)
    html = client.get("/apps/org.gnome.Calculator").text
    assert "flatpak install flathub org.gnome.Calculator" in html
    assert "Liberapay" in html and "Support the creators" in html
    assert "Verified creator" in html and "Sandboxed" in html
    assert "<p>First paragraph.</p>" in html and "<li>one</li>" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html  # description is escaped, never raw HTML
    assert 'src="https://x/shot.png"' in html
    assert "application/ld+json" in html

    hunted = client.get("/apps/io.github.alice.Hunted").text
    assert "flatpak install ./hunted.flatpak" in hunted
    assert "flatpak-builder --user --install" in hunted
    assert "Outside Flathub" in hunted


def test_404s_are_pages_for_humans_and_json_for_the_api(client, session):
    _seed(session)
    r = client.get("/apps/nope.nope.Nope")
    assert r.status_code == 404 and "Nothing on the sonar" in r.text
    assert client.get("/apps/com.spotify.Client").status_code == 404  # proprietary: not listed
    r = client.get("/no/such/page")
    assert r.status_code == 404 and "Nothing on the sonar" in r.text
    r = client.get("/api/apps/nope.nope.Nope")
    assert r.status_code == 404 and r.json()["detail"].endswith("not found")


def test_about_sitemap_feed_robots_health(client, session):
    _seed(session)
    assert "Gate one" in client.get("/about").text
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200 and "application/xml" in sm.headers["content-type"]
    assert "/apps/org.gnome.Calculator</loc>" in sm.text and "spotify" not in sm.text
    feed = client.get("/feed.xml")
    assert "atom+xml" in feed.headers["content-type"] and "<title>Calculator</title>" in feed.text
    assert "Sitemap:" in client.get("/robots.txt").text
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/logo.svg").status_code == 200


def test_paragraphs_and_install_option_helpers():
    html = str(paragraphs("Hello <world>\nsame para\n\n- a\n- b\n\nBye"))
    assert html == "<p>Hello &lt;world&gt; same para</p><ul><li>a</li><li>b</li></ul><p>Bye</p>"
    assert str(paragraphs(None)) == ""

    class S:
        def __init__(self, kind, **kw):
            self.kind, self.priority = kind, {"flathub": 0, "remote": 1, "bundle": 2, "manifest": 3}[kind.value]
            self.remote_name = kw.get("remote_name"); self.remote_url = kw.get("remote_url")
            self.bundle_url = kw.get("bundle_url"); self.manifest_url = kw.get("manifest_url")

    class A:
        app_id = "org.x.Y"
        sources = [S(SourceKind.MANIFEST, manifest_url="https://h/org.x.Y.json"),
                   S(SourceKind.REMOTE, remote_name="alice", remote_url="https://h/alice.flatpakrepo")]

    opts = install_options(A())
    assert [o["kind"] for o in opts] == ["remote", "manifest"]  # priority order, not list order
    assert opts[0]["commands"] == ["flatpak remote-add --if-not-exists alice https://h/alice.flatpakrepo",
                                   "flatpak install alice org.x.Y"]
