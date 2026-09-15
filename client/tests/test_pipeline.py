"""The warn-twice install flow, with flatpak/ostree/clamav replaced by fakes."""

from pathlib import Path

import pytest

from flatsea.api import AppInfo, InstallSource
from flatsea.install import flatpak_cli as fp
from flatsea.install import pipeline
from flatsea.install.scan import ScanResult
from flatsea.state import Decisions

GREEN_META = "[Application]\nname=org.x.Green\n\n[Context]\nsockets=wayland;\nshared=network;\n"
RED_META = "[Application]\nname=org.x.Red\n\n[Context]\nsockets=wayland;\nfilesystems=host;\ndevices=all;\n"


class FakeConfirmer:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def _next(self, name):
        self.calls.append(name)
        return self.answers.pop(0)

    def warn_and_confirm(self, app, report, scan_result):
        return self._next("warn")

    def confirm_again(self, app):
        return self._next("again")

    def confirm_plain(self, app, report):
        return self._next("plain")


class FakeAPI:
    def download(self, url, dest, progress=None):
        Path(dest).write_text("bundle")
        return dest


@pytest.fixture()
def fake_flatpak(monkeypatch, tmp_path):
    """Stub every subprocess touch point; record deploys; hand back a metadata file."""
    calls = []
    meta = {"text": GREEN_META}
    monkeypatch.setattr(fp, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(fp, "available", lambda: True)
    monkeypatch.setattr(fp, "default_arch", lambda: "x86_64")
    monkeypatch.setattr(fp, "ensure_remote", lambda *a, **k: calls.append(("remote-add", a)))
    monkeypatch.setattr(fp, "pull", lambda remote, ref, on_line=None: calls.append(("pull", remote, ref)))
    monkeypatch.setattr(fp, "pull_bundle", lambda path, on_line=None: calls.append(("pull-bundle", str(path))))
    monkeypatch.setattr(fp, "local_refs", lambda app_id, repo=None: [f"flathub:app/{app_id}/x86_64/stable"])

    def _checkout(ref, dest, repo=None):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "metadata").write_text(meta["text"])
        (dest / "files").mkdir(exist_ok=True)
        return dest

    monkeypatch.setattr(fp, "checkout", _checkout)
    monkeypatch.setattr(fp, "deploy", lambda remote, ref, on_line=None: calls.append(("deploy", remote, ref)))
    monkeypatch.setattr(fp, "deploy_bundle", lambda path, on_line=None: calls.append(("deploy-bundle", str(path))))
    monkeypatch.setattr(pipeline, "scan", lambda path: ScanResult(ran=True, scanned_files=3))
    return calls, meta


def _app(app_id="org.x.Green", kind="flathub", perms=()):
    src = {"flathub": InstallSource(kind="flathub", remote_name="flathub", ref=f"app/{app_id}/x86_64/stable"),
           "bundle": InstallSource(kind="bundle", bundle_url="https://example.com/x.flatpak")}[kind]
    return AppInfo(app_id=app_id, name=app_id.split(".")[-1], sources=[src],
                   permissions=[{"arg": p, "level": "green", "reason": p} for p in perms])


def _decisions(tmp_path):
    return Decisions(tmp_path / "decisions.json")


def test_pick_source_prefers_flathub():
    srcs = [InstallSource(kind="manifest"), InstallSource(kind="bundle"), InstallSource(kind="flathub")]
    assert pipeline.pick_source(srcs).kind == "flathub"
    assert pipeline.pick_source([]) is None


def test_green_app_single_confirm_then_deploy(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    conf = FakeConfirmer([True])
    out = pipeline.install(_app(), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed
    assert conf.calls == ["plain"]
    assert [c[0] for c in calls] == ["pull", "deploy"]
    assert out.report.level.label == "green"
    assert not (fp.CACHE / "scan" / "org.x.Green").exists()  # scan dir cleaned up


def test_red_app_needs_two_sures(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    meta["text"] = RED_META
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app("org.x.Red"), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed
    assert conf.calls == ["warn", "again"]
    assert out.report.level.label == "red"
    assert any("--filesystem=host" in r for r in out.report.reasons)
    assert ("deploy", "flathub", "app/org.x.Red/x86_64/stable") in calls


def test_cancel_on_second_dialog_does_not_deploy(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    meta["text"] = RED_META
    conf = FakeConfirmer([True, False])
    out = pipeline.install(_app("org.x.Red"), FakeAPI(), conf, _decisions(tmp_path))
    assert not out.installed and out.cancelled
    assert conf.calls == ["warn", "again"]
    assert not any(c[0].startswith("deploy") for c in calls)


def test_cancel_on_first_dialog(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    meta["text"] = RED_META
    conf = FakeConfirmer([False])
    out = pipeline.install(_app("org.x.Red"), FakeAPI(), conf, _decisions(tmp_path))
    assert out.cancelled and conf.calls == ["warn"]


def test_accepted_warning_is_remembered_until_permissions_change(fake_flatpak, tmp_path):
    _, meta = fake_flatpak
    meta["text"] = RED_META
    decisions = _decisions(tmp_path)
    assert pipeline.install(_app("org.x.Red"), FakeAPI(), FakeConfirmer([True, True]), decisions).installed

    # Same permissions again (an update): no dialogs at all.
    conf = FakeConfirmer([])
    assert pipeline.install(_app("org.x.Red"), FakeAPI(), conf, Decisions(decisions.path)).installed
    assert conf.calls == []

    # Permissions grew: warned again.
    meta["text"] = RED_META + "\n[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n"
    conf = FakeConfirmer([True, True])
    assert pipeline.install(_app("org.x.Red"), FakeAPI(), conf, Decisions(decisions.path)).installed
    assert conf.calls == ["warn", "again"]


def test_clamav_hit_escalates_green_to_red(fake_flatpak, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "scan",
                        lambda path: ScanResult(ran=True, infected=[("files/bin/evil", "Eicar-Test-Signature")]))
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app(), FakeAPI(), conf, _decisions(tmp_path))
    assert out.report.level.label == "red"
    assert conf.calls == ["warn", "again"]
    assert any("Eicar" in r for r in out.report.reasons)


def test_index_permissions_used_when_no_checkout(fake_flatpak, tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "local_refs", lambda app_id, repo=None: [])
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app("org.x.Idx", perms=["--socket=x11"]), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and out.report.level.label == "yellow"
    assert conf.calls == ["warn", "again"]


def test_bundle_source_flow(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    out = pipeline.install(_app("org.x.Green", kind="bundle"), FakeAPI(), FakeConfirmer([True]), _decisions(tmp_path))
    assert out.installed
    assert [c[0] for c in calls] == ["pull-bundle", "deploy-bundle"]


def test_no_source_or_no_flatpak(fake_flatpak, tmp_path, monkeypatch):
    app = _app()
    app.sources = []
    assert "no known install source" in pipeline.install(app, FakeAPI(), FakeConfirmer([]), _decisions(tmp_path)).message
    monkeypatch.setattr(fp, "available", lambda: False)
    assert "not installed" in pipeline.install(_app(), FakeAPI(), FakeConfirmer([]), _decisions(tmp_path)).message
