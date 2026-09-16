"""The Installed page's pure halves: parsing ``flatpak list``, reading the deployed
metadata, and deciding whether an update exists."""

from flatsonar.api import AppInfo
from flatsonar.install import flatpak_cli as fp
from flatsonar.install import pipeline
from flatsonar.install.flatpak_cli import InstalledApp

LIST_OUTPUT = (
    "org.gnome.Calculator\t46.1\tflathub\tsystem\tapp/org.gnome.Calculator/x86_64/stable\tCalculator\n"
    "io.github.alice.Foo\t1.2.0\tflatsonar-local\tuser\tapp/io.github.alice.Foo/x86_64/master\tFoo\n"
    "com.example.Bundle\t\t\tuser\tapp/com.example.Bundle/x86_64/stable\t\n"
    "\n"
)


def test_parse_installed_covers_both_installations_and_blank_columns():
    apps = fp.parse_installed(LIST_OUTPUT)
    assert [a.app_id for a in apps] == ["org.gnome.Calculator", "io.github.alice.Foo", "com.example.Bundle"]
    calc, foo, bundle = apps
    assert (calc.installation, calc.origin, calc.version, calc.name) == ("system", "flathub", "46.1", "Calculator")
    assert foo.locally_built and foo.installation == "user"
    assert bundle.origin == "" and bundle.version == "" and not bundle.locally_built
    assert fp.parse_installed("") == []


def test_deployed_metadata_reads_the_active_deploy(tmp_path, monkeypatch):
    root = tmp_path / "flatpak"
    meta = root / "app" / "org.x.Y" / "current" / "active" / "metadata"
    meta.parent.mkdir(parents=True)
    meta.write_text("[Application]\nname=org.x.Y\n\n[Context]\nsockets=x11;\n", encoding="utf-8")
    monkeypatch.setattr(fp, "FLATPAK_USER_ROOT", root)

    def _no_flatpak(*_a, **_k):
        raise fp.FlatpakError("flatpak not found")

    monkeypatch.setattr(fp, "_run", _no_flatpak)
    assert "sockets=x11;" in fp.deployed_metadata("org.x.Y", "user")
    assert fp.deployed_metadata("org.x.Missing", "user") is None


def test_version_newer():
    assert pipeline.version_newer("1.2.1", "1.2.0")
    assert pipeline.version_newer("v2.0", "1.9.9")
    assert pipeline.version_newer("46.1", "46")
    assert not pipeline.version_newer("1.2.0", "1.2.0")
    assert not pipeline.version_newer("1.2.0", "1.2.1")
    assert not pipeline.version_newer(None, "1.0")
    assert not pipeline.version_newer("1.0", "")


def test_update_available_rule():
    remote = InstalledApp("org.x.A", version="1.0", origin="flathub")
    local = InstalledApp("org.x.B", version="1.0", origin=fp.LOCAL_REMOTE)
    bundle = InstalledApp("org.x.C", version="1.0", origin="")
    # Remote-installed: only what flatpak reported counts, whatever the index says.
    assert pipeline.update_available(AppInfo("org.x.A", "A", latest_version="0.1"), remote, {"org.x.A"})
    assert not pipeline.update_available(AppInfo("org.x.A", "A", latest_version="9.0"), remote, set())
    # Local builds and bundles: the index's latest version is the only signal.
    assert pipeline.update_available(AppInfo("org.x.B", "B", latest_version="1.1"), local, set())
    assert not pipeline.update_available(AppInfo("org.x.B", "B", latest_version="1.0"), local, {"org.x.B"})
    assert pipeline.update_available(AppInfo("org.x.C", "C", latest_version="2.0"), bundle, set())
    assert not pipeline.update_available(AppInfo.unknown("org.x.C"), bundle, set())


def test_unknown_app_defaults_are_honest():
    assert AppInfo.unknown("com.x.Foo", origin="flathub").trust == "reviewed"
    assert AppInfo.unknown("com.x.Foo", origin="flathub").on_flathub
    assert AppInfo.unknown("com.x.Foo", origin="alice").trust == "unverified"
    assert AppInfo.unknown("com.x.Foo").name == "Foo"
    assert AppInfo.unknown("com.x.Foo", name="Foo App").name == "Foo App"
