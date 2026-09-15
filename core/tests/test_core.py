import pytest

from flatsea_core import (
    RiskLevel,
    is_open_source,
    parse_manifest_text,
    score_finish_args,
)
from flatsea_core.manifest import ManifestError, looks_like_manifest

# --- fixtures shaped like real Flathub manifests -----------------------------

GIMP_JSON = """
{
    // GIMP-style manifest with comments, nested modules, several sources
    "app-id": "org.gimp.GIMP",
    "runtime": "org.gnome.Platform",
    "runtime-version": "47",
    "sdk": "org.gnome.Sdk",
    "command": "gimp",
    "finish-args": [
        "--share=ipc",
        "--socket=fallback-x11",
        "--socket=wayland",
        "--device=dri",
        "--filesystem=host",
        "--filesystem=xdg-config/GIMP",
        "--talk-name=org.gtk.vfs.*",
        "--talk-name=org.freedesktop.FileManager1"
    ],
    "modules": [
        { "name": "babl", "sources": [{ "type": "git", "url": "https://gitlab.gnome.org/GNOME/babl.git", "tag": "BABL_0_1_110" }] },
        { "name": "gegl", "modules": [
            { "name": "libnsgif", "sources": [{ "type": "archive", "url": "https://download.netsurf-browser.org/libs/releases/libnsgif-1.0.0-src.tar.gz" }] }
        ], "sources": [{ "type": "git", "url": "https://gitlab.gnome.org/GNOME/gegl.git" }] },
        { "name": "gimp", "sources": [{ "type": "git", "url": "https://gitlab.gnome.org/GNOME/gimp.git", "commit": "abc123" }] }
    ]
}
"""

CALC_YAML = """
app-id: org.gnome.Calculator
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: gnome-calculator
finish-args:
  - --share=ipc
  - --share=network
  - --socket=fallback-x11
  - --socket=wayland
  - --device=dri
  - --metadata=X-DConf=migrate-path=/org/gnome/calculator/
modules:
  - name: gnome-calculator
    buildsystem: meson
    sources:
      - type: git
        url: https://gitlab.gnome.org/GNOME/gnome-calculator.git
        tag: 47.0
"""

ESCAPE_YAML = """
app-id: com.example.Sketchy
runtime: org.freedesktop.Platform
runtime-version: '24.08'
sdk: org.freedesktop.Sdk
command: sketchy
finish-args:
  - --socket=x11
  - --talk-name=org.freedesktop.Flatpak
  - --filesystem=~/.ssh:ro
  - --env=LD_PRELOAD=/app/lib/hook.so
modules:
  - name: sketchy
    sources:
      - type: archive
        url: https://example.com/sketchy.tar.gz
"""


# --- manifest -------------------------------------------------------------------


def test_parse_json_with_comments_and_nested_modules():
    m = parse_manifest_text(GIMP_JSON, "org.gimp.GIMP.json")
    assert m.app_id == "org.gimp.GIMP"
    assert m.runtime == "org.gnome.Platform"
    assert m.runtime_version == "47"
    assert m.modules == ["babl", "gegl", "libnsgif", "gimp"]
    assert len(m.sources) == 4
    # last module's git source is the best upstream guess
    assert m.upstream_urls[0] == "https://gitlab.gnome.org/GNOME/gimp.git"


def test_parse_yaml():
    m = parse_manifest_text(CALC_YAML, "org.gnome.Calculator.yml")
    assert m.app_id == "org.gnome.Calculator"
    assert m.command == "gnome-calculator"
    assert "--socket=wayland" in m.finish_args
    assert m.sources[0].tag == "47.0"


def test_parse_rejects_non_manifest():
    with pytest.raises(ManifestError):
        parse_manifest_text("{}")
    with pytest.raises(ManifestError):
        parse_manifest_text("app-id: notreversedns\nmodules: []\n")
    with pytest.raises(ManifestError):
        parse_manifest_text("- just\n- a list\n")


def test_looks_like_manifest_prefilter():
    assert looks_like_manifest(CALC_YAML)
    assert not looks_like_manifest("name: something\nversion: 1\n")


# --- risk -------------------------------------------------------------------------


def test_host_filesystem_is_red():
    m = parse_manifest_text(GIMP_JSON, "x.json")
    r = score_finish_args(m.finish_args, m.app_id)
    assert r.level == RiskLevel.RED
    assert any("--filesystem=host" in s for s in r.reasons)


def test_wayland_only_app_is_green():
    m = parse_manifest_text(CALC_YAML, "x.yml")
    r = score_finish_args(m.finish_args, m.app_id)
    assert r.level == RiskLevel.GREEN
    assert r.reasons == []
    assert not r.is_risky
    # every arg still gets a description for the permission badges
    assert len(r.findings) == len(m.finish_args)


def test_x11_alone_is_yellow():
    r = score_finish_args(["--socket=x11", "--share=ipc"])
    assert r.level == RiskLevel.YELLOW


def test_sandbox_escape_and_creds_are_red():
    m = parse_manifest_text(ESCAPE_YAML, "x.yml")
    r = score_finish_args(m.finish_args, m.app_id)
    assert r.level == RiskLevel.RED
    reds = [f for f in r.findings if f.level == RiskLevel.RED]
    assert {f.arg for f in reds} == {
        "--talk-name=org.freedesktop.Flatpak",
        "--filesystem=~/.ssh:ro",
        "--env=LD_PRELOAD=/app/lib/hook.so",
    }


@pytest.mark.parametrize(
    "arg,level",
    [
        ("--filesystem=host:ro", RiskLevel.YELLOW),
        ("--filesystem=home", RiskLevel.YELLOW),
        ("--filesystem=xdg-download", RiskLevel.GREEN),
        ("--filesystem=xdg-config", RiskLevel.YELLOW),
        ("--filesystem=xdg-config/autostart", RiskLevel.RED),
        ("--filesystem=xdg-run/docker.sock", RiskLevel.RED),
        ("--device=all", RiskLevel.RED),
        ("--device=dri", RiskLevel.GREEN),
        ("--socket=session-bus", RiskLevel.RED),
        ("--socket=system-bus", RiskLevel.RED),
        ("--talk-name=org.freedesktop.portal.Desktop", RiskLevel.GREEN),
        ("--talk-name=org.freedesktop.secrets", RiskLevel.YELLOW),
        ("--talk-name=org.*", RiskLevel.RED),
        ("--own-name=org.mpris.MediaPlayer2.foo", RiskLevel.GREEN),
        ("--own-name=org.freedesktop.Notifications", RiskLevel.RED),
        ("--env=GTK_THEME=Adwaita", RiskLevel.GREEN),
        ("--persist=.", RiskLevel.GREEN),
        ("--allow=devel", RiskLevel.YELLOW),
        ("--nofilesystem=host", RiskLevel.GREEN),
        ("--frobnicate=1", RiskLevel.YELLOW),
    ],
)
def test_individual_args(arg, level):
    assert score_finish_args([arg]).level == level


def test_own_name_of_self_is_green():
    r = score_finish_args(["--own-name=org.example.App.Helper"], app_id="org.example.App")
    assert r.level == RiskLevel.GREEN


def test_escalate_with_clamav_hit():
    r = score_finish_args(["--socket=wayland"])
    r.escalate(RiskLevel.RED, "clamav", "Eicar-Test-Signature found")
    assert r.level == RiskLevel.RED
    assert r.reasons == ["[red] clamav: Eicar-Test-Signature found"]


# --- spdx -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,ok",
    [
        ("GPL-3.0-or-later", True),
        ("GPL-3.0+", True),
        ("MIT", True),
        ("Apache-2.0 WITH LLVM-exception", True),
        ("GPL-2.0-only AND LGPL-2.1-or-later", True),
        ("(MIT OR Apache-2.0)", True),
        ("LicenseRef-proprietary", False),
        ("LicenseRef-proprietary=https://example.com/eula", False),
        ("MIT AND LicenseRef-proprietary", False),
        ("Proprietary", False),
        ("", False),
        (None, False),
        ("AND", False),
    ],
)
def test_is_open_source(expr, ok):
    assert is_open_source(expr) is ok
