"""Unfilled templates: values a project's release tooling substitutes before publishing
(``VERSION_PLACEHOLDER``, ``@APP_NAME@``, ``${VERSION}``, ``{{ version }}``,
``__TARBALL_SHA256__``) but that are committed to the repository in their raw form.

A crawler reading the repository sees the template, not what gets released. A manifest
source that fetches ``releases/download/release-VERSION_PLACEHOLDER-tag/...`` can never
download, and a ``sha256: CHECKSUM_X64_PLACEHOLDER`` can never verify, so such a
manifest must not be offered as something to build; a metainfo ``<name>@APP_NAME@</name>``
is not the app's name.
"""

from __future__ import annotations

import re

from .manifest import Manifest

# Substitution syntax: never meant literally, in a manifest field or in an app's name.
_TEMPLATE = re.compile(
    r"\b[A-Z0-9_]*PLACEHOLDER[A-Z0-9_]*\b"  # VERSION_PLACEHOLDER
    r"|@[A-Za-z_][A-Za-z0-9_]*@"  # meson / autotools configure_file: @APP_NAME@
    r"|\$\{[^}\s]+\}"  # ${VERSION}: flatpak-builder never expands these in a source
    r"|\{\{[^{}]*\}\}"  # {{ version }}: Jinja / Mustache
    r"|\b__[A-Z][A-Z0-9_]*__\b"  # __FIREFOX_TAR_SHA256__
)
# Stand-ins people type into a field they mean to fill in later. Only checked in manifest
# source fields: in a summary, "A TODO list" is just words.
_STAND_IN = re.compile(
    r"\bplaceholder(?:[-_][A-Za-z0-9]+)*\b"  # placeholder-update-before-release
    r"|<[A-Z][A-Z0-9_ -]*>"  # <COMMIT>, <SHA256>
    r"|\b(?:[A-Z0-9]+_)*(?:REPLACE|TODO|FIXME|CHANGEME)(?:_[A-Z0-9]+)*\b"  # REPLACE_WITH_REAL_SHA256
)
_HEX = re.compile(r"[0-9a-fA-F]+")
_CHECKSUM_LEN = {"sha256": 64, "sha512": 128, "sha1": 40, "md5": 32}


def placeholders(value: str | None, stand_ins: bool = False) -> list[str]:
    """The template tokens in ``value``, in order; [] when there are none. ``stand_ins``
    also counts TODO / REPLACE_WITH_... / <COMMIT>-style fill-me-ins."""
    if not value:
        return []
    found = [(m.start(), m.group(0)) for m in _TEMPLATE.finditer(value)]
    if stand_ins:
        found += [(m.start(), m.group(0)) for m in _STAND_IN.finditer(value)]
    return [token for _, token in sorted(found)]


def unfilled(value: str | None) -> bool:
    """Is this metadata value (a name, summary, version) a template nobody filled in?"""
    return bool(placeholders(value))


def manifest_template_problems(m: Manifest) -> list[str]:
    """Why this manifest cannot be built as committed, one short phrase per problem;
    [] when nothing is wrong. Only fields flatpak-builder takes literally are checked
    (sources, runtime, sdk): build commands legitimately use ``${FLATPAK_DEST}``. A git
    ``commit`` need not look like a hash - flatpak-builder resolves tags there."""
    problems: list[str] = []

    def note(text: str) -> None:
        if text not in problems:
            problems.append(text)

    for label, value in (("runtime", m.runtime), ("runtime-version", m.runtime_version), ("sdk", m.sdk)):
        for token in placeholders(value, stand_ins=True):
            note(f"{token} as the {label}")
    for src in m.sources:
        for label, value in (("source URL", src.url), ("source tag", src.tag), ("source branch", src.branch),
                             ("source commit", src.commit)):
            for token in placeholders(value, stand_ins=True):
                note(f"{token} in a {label}")
        # A malformed checksum fails verification however it got there: a template,
        # "SKIP", or a sha1 pasted where the sha256 goes.
        want = _CHECKSUM_LEN.get(src.checksum_type or "")
        if src.checksum and want and not (len(src.checksum) == want and _HEX.fullmatch(src.checksum)):
            note(f"{src.checksum[:40]!r} is not a valid {src.checksum_type}")
    return problems


def describe_template_problems(problems: list[str], limit: int = 3) -> str:
    """``"the manifest can't be built as committed: VERSION_PLACEHOLDER in a source URL, ..."``"""
    shown = ", ".join(problems[:limit])
    more = f" and {len(problems) - limit} more" if len(problems) > limit else ""
    return f"the manifest can't be built as committed: {shown}{more}"
