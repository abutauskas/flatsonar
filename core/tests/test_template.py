"""Unfilled release templates committed to a repository: never offered as a build,
never shown as an app's name or version."""

from flatsonar_core import (
    describe_template_problems,
    manifest_template_problems,
    parse_manifest_text,
    unfilled,
)

HEAD = """
app-id: org.x.App
runtime: org.gnome.Platform
runtime-version: '50'
sdk: org.gnome.Sdk
command: app
modules:
  - name: app
    buildsystem: simple
    build-commands: ['install -Dm755 app ${FLATPAK_DEST}/bin/app']
    sources:
"""


def _problems(sources: str) -> list[str]:
    return manifest_template_problems(parse_manifest_text(HEAD + sources, "org.x.App.yml"))


def test_termix_style_template():
    problems = _problems("""
      - type: file
        url: https://github.com/x/app/releases/download/release-VERSION_PLACEHOLDER-tag/app_x64.AppImage
        sha256: CHECKSUM_X64_PLACEHOLDER
""")
    assert problems == ["VERSION_PLACEHOLDER in a source URL", "'CHECKSUM_X64_PLACEHOLDER' is not a valid sha256"]
    assert describe_template_problems(problems).startswith("the manifest can't be built as committed: VERSION_")


def test_a_buildable_manifest_has_no_problems():
    # A tag where the commit goes resolves fine in flatpak-builder (several Flathub apps
    # do this); ${FLATPAK_DEST} in build-commands is expanded at build time.
    assert _problems("""
      - type: git
        url: https://github.com/x/app.git
        commit: v4.3.1
      - type: archive
        url: https://example.org/app-1.0.tar.xz
        sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
      - type: git
        url: git@github.com:x/dep.git
        branch: main
""") == []


def test_every_template_style_seen_in_the_wild():
    cases = {
        "url: '{{APPIMAGE_URL}}'": "{{APPIMAGE_URL}} in a source URL",
        "url: https://x/@APPIMAGE_PATH@": "@APPIMAGE_PATH@ in a source URL",
        "url: https://x/a.git\n        branch: ${oc.env:CI_COMMIT_BRANCH,main}": "${oc.env:CI_COMMIT_BRANCH,main} in a source branch",
        "url: https://x/a.git\n        commit: '<COMMIT>'": "<COMMIT> in a source commit",
        "url: https://x/a.git\n        commit: COMMIT_SHA_TO_REPLACE": "COMMIT_SHA_TO_REPLACE in a source commit",
        "url: https://x/a.git\n        commit: placeholder-pin-to-actual-commit-sha":
            "placeholder-pin-to-actual-commit-sha in a source commit",
        "url: https://x/a.tar.gz\n        sha256: __FIREFOX_TAR_SHA256__": "'__FIREFOX_TAR_SHA256__' is not a valid sha256",
        "url: https://x/a.tar.gz\n        sha256: SKIP": "'SKIP' is not a valid sha256",
        # a sha1 pasted where the sha256 goes fails verification just the same
        "url: https://x/a.tar.gz\n        sha256: baee400fa9ced6f5481a728138fed6e867b0ff7f":
            "'baee400fa9ced6f5481a728138fed6e867b0ff7f' is not a valid sha256",
    }
    for body, expected in cases.items():
        kind = "git" if "a.git" in body else "archive"
        assert _problems(f"\n      - type: {kind}\n        {body}\n") == [expected], body


def test_unfilled_metadata_values():
    for value in ("@APP_NAME@", "VERSION_PLACEHOLDER", "{{ version }}", "${PROJECT_VERSION}", "__NAME__"):
        assert unfilled(value), value
    # Stand-in words only count in manifest fields: these are ordinary text.
    for value in ("A TODO list manager", "Firefox", "2.8.0", "user@example.org", "C++ IDE", None, ""):
        assert not unfilled(value), value
