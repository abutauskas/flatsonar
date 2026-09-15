from flatsonar_core import parse_manifest_text

from flatsonar_server.crawler import funding
from flatsonar_server.crawler.appstream import parse_metainfo
from flatsonar_server.crawler.base import Candidate, SourceSpec, upsert_candidate
from flatsonar_server.crawler.credit import normalise_repo_url
from flatsonar_server.crawler.flathub import permissions_to_finish_args
from flatsonar_server.crawler.forge import find_icon, find_manifests, find_metainfo
from flatsonar_server.models import App, SourceKind

MANIFEST = """
app-id: com.example.Hunted
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: hunted
finish-args: [--socket=wayland, --filesystem=host]
modules:
  - name: hunted
    sources:
      - type: git
        url: https://github.com/alice/hunted.git
"""


def _flathub_candidate(app_id="org.gnome.Calculator", **kw) -> Candidate:
    m = parse_manifest_text(MANIFEST.replace("com.example.Hunted", app_id))
    m.finish_args = ["--socket=wayland", "--share=network"]
    base = dict(
        app_id=app_id, name="Calculator", summary="Does sums", description="Long text", license="GPL-3.0-or-later",
        is_oss=True, developer_name="The GNOME Project", upstream_url="https://gitlab.gnome.org/GNOME/gnome-calculator",
        categories=["Utility"], on_flathub=True, flathub_verified=True, manifest=m,
        sources=[SourceSpec(kind=SourceKind.FLATHUB, remote_name="flathub", ref=f"app/{app_id}/x86_64/stable")],
    )
    base.update(kw)
    return Candidate(**base)


# --- upsert -----------------------------------------------------------------------


def test_upsert_creates_and_scores(session):
    app, created = upsert_candidate(session, _flathub_candidate())
    session.commit()
    assert created
    assert app.risk_level == "green"
    assert app.permissions[0]["arg"] == "--socket=wayland"
    assert [s.kind for s in app.sources] == [SourceKind.FLATHUB]
    assert app.manifest.runtime == "org.gnome.Platform"


def test_forge_only_enriches_flathub_apps(session):
    upsert_candidate(session, _flathub_candidate())
    session.commit()
    m = parse_manifest_text(MANIFEST.replace("com.example.Hunted", "org.gnome.Calculator"))
    forge = Candidate(
        app_id="org.gnome.Calculator", name="gnome-calculator (repo name)", summary="worse summary",
        stars=420, sponsor_links=[{"platform": "liberapay", "url": "https://liberapay.com/gnome"}],
        manifest=m, is_oss=True, sources=[SourceSpec(kind=SourceKind.MANIFEST, manifest_url="https://x/y.yml")],
    )
    app, created = upsert_candidate(session, forge)
    session.commit()
    assert not created
    assert app.name == "Calculator"  # Flathub data kept
    assert app.stars == 420  # but stars and sponsors merged in
    assert app.sponsor_links[0]["platform"] == "liberapay"
    assert app.risk_level == "green"  # forge manifest (--filesystem=host) did not override deployed perms
    assert [s.kind for s in app.sources] == [SourceKind.FLATHUB]


def test_off_flathub_app_gets_manifest_source_and_red(session):
    m = parse_manifest_text(MANIFEST)
    cand = Candidate(
        app_id="com.example.Hunted", name="Hunted", license="MIT", stars=3, manifest=m,
        manifest_url="https://raw.githubusercontent.com/alice/hunted/main/com.example.Hunted.yml",
        sources=[
            SourceSpec(kind=SourceKind.BUNDLE, bundle_url="https://github.com/alice/hunted/releases/x.flatpak"),
            SourceSpec(kind=SourceKind.MANIFEST, manifest_url="https://raw/x.yml"),
        ],
    )
    app, _ = upsert_candidate(session, cand)
    session.commit()
    assert app.is_oss is True  # derived from license
    assert app.risk_level == "red"
    assert app.upstream_url == "https://github.com/alice/hunted.git"
    assert [s.kind for s in app.sources] == [SourceKind.BUNDLE, SourceKind.MANIFEST]


# --- api ----------------------------------------------------------------------------


def test_api_list_search_detail(client, session):
    upsert_candidate(session, _flathub_candidate())
    upsert_candidate(session, _flathub_candidate("com.spotify.Client", name="Spotify",
                                                 license="LicenseRef-proprietary", is_oss=False))
    session.commit()

    page = client.get("/api/apps").json()
    assert page["total"] == 1  # proprietary hidden by default
    assert page["items"][0]["app_id"] == "org.gnome.Calculator"
    assert client.get("/api/apps?oss_only=false").json()["total"] == 2
    assert client.get("/api/apps?q=sums").json()["total"] == 1
    assert client.get("/api/apps?q=nothing").json()["total"] == 0
    assert client.get("/api/apps?category=Utility").json()["total"] == 1
    assert client.get("/api/apps?risk=red").json()["total"] == 0

    d = client.get("/api/apps/org.gnome.Calculator").json()
    assert d["developer_name"] == "The GNOME Project"
    assert d["sources"][0]["kind"] == "flathub"
    assert d["permissions"][0]["level"] == "green"
    assert client.get("/api/apps/nope.nope.Nope").status_code == 404

    stats = client.get("/api/stats").json()
    assert stats["apps"] == 1 and stats["on_flathub"] == 1
    assert client.get("/api/categories").json() == [{"name": "Utility", "count": 1}]
    assert client.get("/api/apps/org.gnome.Calculator/manifest").json()["finish_args"] == [
        "--socket=wayland", "--share=network"]


# --- helpers ----------------------------------------------------------------------


def test_permissions_to_finish_args():
    perms = {
        "shared": ["ipc", "network"], "sockets": ["wayland"], "devices": ["dri"],
        "filesystems": ["xdg-config/GIMP:create", "host"],
        "session-bus": {"talk": ["org.gtk.vfs.*"], "own": ["org.mpris.MediaPlayer2.gimp"]},
        "system-bus": {"talk": ["org.freedesktop.login1"]},
        "unset-environment": ["GTK_MODULES"],
    }
    out = permissions_to_finish_args(perms)
    assert "--filesystem=host" in out
    assert "--talk-name=org.gtk.vfs.*" in out
    assert "--own-name=org.mpris.MediaPlayer2.gimp" in out
    assert "--system-talk-name=org.freedesktop.login1" in out
    assert "--unset-env=GTK_MODULES" in out
    assert permissions_to_finish_args(None) == []


def test_funding_yml():
    links = funding.parse_funding_yml("github: [alice, bob]\nko_fi: alice\ncustom: ['https://alice.dev/donate']\nliberapay: \n")
    assert {"platform": "github", "url": "https://github.com/sponsors/alice"} in links
    assert {"platform": "ko_fi", "url": "https://ko-fi.com/alice"} in links
    assert {"platform": "custom", "url": "https://alice.dev/donate"} in links
    assert len(links) == 4
    assert funding.donation_link("https://www.patreon.com/foo") == [{"platform": "patreon", "url": "https://www.patreon.com/foo"}]
    assert funding.merge(links, funding.donation_link("https://ko-fi.com/alice")) == links


def test_credit_url_normalisation():
    u = normalise_repo_url("git@github.com:alice/hunted.git")
    assert (u.url, u.forge, u.owner, u.repo) == ("https://github.com/alice/hunted", "github", "alice", "hunted")
    u = normalise_repo_url("https://gitlab.gnome.org/GNOME/gnome-calculator/-/tree/main")
    assert (u.url, u.owner, u.repo) == ("https://gitlab.gnome.org/GNOME/gnome-calculator", "GNOME", "gnome-calculator")
    assert normalise_repo_url("not a url") is None


def test_forge_tree_helpers():
    tree = [
        "README.md", "build-aux/flatpak/org.gnome.Foo.Devel.json", "build-aux/flatpak/org.gnome.Foo.json",
        "node_modules/x/com.bad.Thing.json", "data/org.gnome.Foo.metainfo.xml.in",
        "data/icons/hicolor/scalable/apps/org.gnome.Foo.svg", "data/icons/hicolor/64x64/apps/org.gnome.Foo.png",
        "data/icons/hicolor/symbolic/apps/org.gnome.Foo-symbolic.svg",
    ]
    assert find_manifests(tree) == ["build-aux/flatpak/org.gnome.Foo.json", "build-aux/flatpak/org.gnome.Foo.Devel.json"]
    assert find_metainfo(tree, "org.gnome.Foo") == "data/org.gnome.Foo.metainfo.xml.in"
    assert find_icon(tree, "org.gnome.Foo") == "data/icons/hicolor/scalable/apps/org.gnome.Foo.svg"


def test_metainfo_parse():
    xml = """<?xml version="1.0"?>
    <component type="desktop-application">
      <id>org.gnome.Foo</id>
      <name>Foo</name><name xml:lang="de">Fu</name>
      <summary>Does foo</summary>
      <project_license>GPL-3.0-or-later</project_license>
      <developer id="org.gnome"><name>Alice</name></developer>
      <url type="homepage">https://foo.example</url>
      <url type="donation">https://liberapay.com/alice</url>
      <description><p>Para one.</p><ul><li>Point</li></ul><p xml:lang="de">Nein</p></description>
      <screenshots><screenshot type="default"><image>https://foo.example/1.png</image></screenshot></screenshots>
      <categories><category>Utility</category></categories>
      <releases><release version="1.2" date="2026-01-01"/></releases>
    </component>"""
    mi = parse_metainfo(xml)
    assert mi.app_id == "org.gnome.Foo" and mi.name == "Foo" and mi.developer_name == "Alice"
    assert mi.description == "Para one.\n- Point"
    assert mi.donation == "https://liberapay.com/alice"
    assert mi.screenshots == ["https://foo.example/1.png"]
    assert mi.latest_version == "1.2"
    assert parse_metainfo("<not xml") is None
