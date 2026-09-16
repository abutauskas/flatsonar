"""The simulated flatpak used for previews on machines without one. These run the real
pipeline against it, so they are also an end-to-end check of install / update / remove
with a second backend."""

from pathlib import Path

import pytest

from flatsonar_core import metadata_to_finish_args

from flatsonar.api import AppInfo, InstallSource
from flatsonar.install import fake_flatpak as ff
from flatsonar.install import flatpak_cli as fp
from flatsonar.install import pipeline
from flatsonar.install.scan import ScanResult
from flatsonar.state import Decisions

from test_pipeline import CLEAN_MANIFEST, FakeAPI, FakeConfirmer


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(fp, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(ff, "PACE", 0.0)
    monkeypatch.setattr(pipeline, "scan", lambda path: ScanResult(ran=True, scanned_files=2))
    # Record the real functions so monkeypatch puts them back after activate() swaps them.
    for name in ff._PATCHED:
        monkeypatch.setattr(fp, name, getattr(fp, name))
    return ff.activate(reset=True, path=tmp_path / "fake.json")


def _installed(fake, app_id):
    return next(a for a in fake.installed_apps() if a.app_id == app_id)


def _index_app(app_id, name, **kw):
    return AppInfo(app_id=app_id, name=name, trust="verified",
                   sources=[InstallSource(kind="flathub", remote_name="flathub", ref=f"app/{app_id}/x86_64/stable")],
                   **kw)


def test_metadata_round_trips_through_the_real_parser():
    args = ["--share=network", "--socket=wayland", "--filesystem=host", "--talk-name=org.freedesktop.Flatpak",
            "--own-name=org.x.Y", "--system-talk-name=org.freedesktop.login1", "--env=LD_PRELOAD=x", "--persist=."]
    back = metadata_to_finish_args(ff.metadata_from_finish_args("org.x.Y", args))
    assert set(back) == set(args)


def test_scenario_covers_every_installed_page_path(fake):
    apps = {a.app_id: a for a in fp.installed_apps()}
    assert len(apps) == 5
    assert apps["ai.jan.Jan"].installation == "system"
    assert apps["garden.turtle.Jellybean"].locally_built
    assert apps["com.example.Legacy"].origin == ""
    assert fp.updates_available() == {"ai.jan.Jan", "app.cantara.Cantara"}  # local builds have no remote
    assert "--device=all" in metadata_to_finish_args(fp.deployed_metadata("com.example.Legacy"))
    assert fp.deployed_metadata("nope.Nope") is None


def test_update_that_changes_nothing_asks_nothing(fake, tmp_path):
    conf = FakeConfirmer([])
    out = pipeline.update(_index_app("ai.jan.Jan", "Jan"), _installed(fake, "ai.jan.Jan"), FakeAPI(), conf,
                          Decisions(tmp_path / "d.json"))
    assert out.installed and conf.calls == [] and out.scan.ran
    jan = _installed(fake, "ai.jan.Jan")
    assert jan.version == "0.8.4" and jan.installation == "system"
    assert "ai.jan.Jan" not in fp.updates_available()


def test_update_that_grows_the_sandbox_warns_twice(fake, tmp_path):
    app, inst = _index_app("app.cantara.Cantara", "Cantara"), _installed(fake, "app.cantara.Cantara")
    conf = FakeConfirmer([True, False])
    out = pipeline.update(app, inst, FakeAPI(), conf, Decisions(tmp_path / "d.json"))
    assert out.cancelled and conf.calls == ["warn", "again"]
    assert any("--filesystem=host" in r for r in out.report.reasons)
    assert _installed(fake, "app.cantara.Cantara").version == "2.7.0"  # untouched

    conf = FakeConfirmer([True, True])
    out = pipeline.update(app, inst, FakeAPI(), conf, Decisions(tmp_path / "d.json"))
    assert out.installed and conf.calls == ["warn", "again"]
    assert _installed(fake, "app.cantara.Cantara").version == "2.7.1"
    assert "filesystems=host;" in fp.deployed_metadata("app.cantara.Cantara")


def test_install_unknown_app_uses_index_permissions_then_uninstall(fake, tmp_path):
    app = _index_app("org.new.App", "New", permissions=[{"arg": "--socket=x11", "level": "yellow", "reason": "x"}])
    conf = FakeConfirmer([True, True])
    result = pipeline.install(app, FakeAPI(), conf, Decisions(tmp_path / "d.json"))
    assert result.installed and conf.calls == ["warn", "again"]
    assert not result.scan.ran  # nothing to check out: the fake knows no files for it
    assert "org.new.App" in fp.installed_ids()
    fp.uninstall("org.new.App")
    assert "org.new.App" not in fp.installed_ids()


def test_local_build_update_rebuilds_from_manifest(fake, tmp_path):
    app = AppInfo(app_id="garden.turtle.Jellybean", name="Stockpile", trust="unverified", latest_version="0.5.1",
                  sources=[InstallSource(kind="manifest", manifest_url="https://x/garden.turtle.Jellybean.yml")])
    inst = _installed(fake, "garden.turtle.Jellybean")
    assert pipeline.update_available(app, inst, set())
    conf = FakeConfirmer([True, True])  # unverified publisher: gate one
    out = pipeline.update(app, inst, FakeAPI(CLEAN_MANIFEST.format(app_id="garden.turtle.Jellybean")), conf,
                          Decisions(tmp_path / "d.json"))
    assert out.installed and out.message == "Stockpile updated."
    after = _installed(fake, "garden.turtle.Jellybean")
    assert after.version == "0.5.1" and after.locally_built
    assert not pipeline.update_available(app, after, set())
    assert metadata_to_finish_args(fp.deployed_metadata("garden.turtle.Jellybean")) == ["--socket=wayland"]


def test_state_persists_and_reset_reseeds(fake, tmp_path):
    fp.uninstall("com.example.Legacy")
    again = ff.FakeFlatpak(tmp_path / "fake.json")
    assert again.load() and "com.example.Legacy" not in again.apps
    again.seed()
    assert "com.example.Legacy" in again.apps
    assert Path(tmp_path / "fake.json").exists()
