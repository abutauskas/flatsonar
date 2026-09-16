"""Parse flatpak-builder manifests (JSON or YAML) into a small, stable dataclass.

Only the fields Flatsonar cares about are kept: app id, runtime/sdk, finish-args
(sandbox permissions), and the upstream sources so we can credit the original
authors.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# flatpak-builder JSON manifests allow // and /* */ comments.
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Reverse-DNS app id: at least three dot-separated components.
APP_ID_RE = re.compile(r"^[A-Za-z_][\w\-]*(\.[A-Za-z_][\w\-]*){2,}$")


class ManifestError(ValueError):
    """Raised when text is not a usable flatpak manifest."""


@dataclass(frozen=True)
class ManifestSource:
    kind: str  # git, archive, file, dir, script, shell, patch, extra-data, ...
    url: str | None = None
    tag: str | None = None
    commit: str | None = None
    branch: str | None = None
    checksum: str | None = None  # sha256 / sha512 / sha1 / md5, whichever was given
    commands: tuple[str, ...] = ()  # shell / script sources
    dest_filename: str | None = None

    @property
    def pinned(self) -> bool:
        """Can the bytes this source fetches change without the manifest changing?"""
        if self.kind == "git":
            return bool(self.commit or self.tag)
        if self.kind in ("archive", "file", "extra-data"):
            return bool(self.checksum) or not self.url  # no url: a file next to the manifest
        return True  # inline, patch, dir, shell, script: content is in the manifest / repo


@dataclass
class Manifest:
    app_id: str
    runtime: str | None = None
    runtime_version: str | None = None
    sdk: str | None = None
    command: str | None = None
    branch: str | None = None
    finish_args: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)  # module names, in order
    sources: list[ManifestSource] = field(default_factory=list)  # flattened, all modules
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def upstream_urls(self) -> list[str]:
        """Git/archive URLs of the sources, most-likely-upstream first.

        Heuristic: the last module in a manifest is almost always the app itself
        (flatpak-builder builds dependencies first), so its git source is the
        best guess for "who made this".
        """
        urls = [s.url for s in reversed(self.sources) if s.url and s.kind == "git"]
        urls += [s.url for s in reversed(self.sources) if s.url and s.kind == "archive"]
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out


def _strip_json_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub("", text)
    return _LINE_COMMENT.sub("", text)


def _json_safe(value: Any) -> Any:
    """PyYAML's ``safe_load`` turns an unquoted ``2024-01-02`` into a real
    ``date``/``datetime``, which the stdlib ``json`` module (and the SQLite/Postgres
    JSON column this ends up in) cannot serialise. Manifests are written by hundreds
    of different projects; walk the parsed tree and make everything JSON-native
    rather than special-case the one field that happened to trip on it first."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _load(text: str, hint: str | None = None) -> Any:
    stripped = text.lstrip()
    looks_json = stripped.startswith("{") or (hint or "").endswith(".json")
    if looks_json:
        try:
            return json.loads(_strip_json_comments(text))
        except json.JSONDecodeError as exc:
            if not hint or hint.endswith(".json"):
                raise ManifestError(f"invalid JSON manifest: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML manifest: {exc}") from exc


def _iter_modules(modules: Any) -> list[dict[str, Any]]:
    """Flatten nested modules (flatpak-builder allows modules inside modules).

    String entries reference external files we can't resolve here; they are
    kept as a stub with only a name so ordering is preserved.
    """
    out: list[dict[str, Any]] = []
    for m in modules or []:
        if isinstance(m, str):
            out.append({"name": Path(m).stem, "_external": True})
        elif isinstance(m, dict):
            out.append(m)
            out.extend(_iter_modules(m.get("modules")))
    return out


def _parse_sources(module: dict[str, Any]) -> list[ManifestSource]:
    out = []
    for s in module.get("sources") or []:
        if not isinstance(s, dict):
            continue
        # YAML happily parses `tag: 1.0` as a float; everything here is a string.
        def _s(key: str) -> str | None:
            v = s.get(key)
            return None if v is None else str(v)

        checksum = next((_s(k) for k in ("sha256", "sha512", "sha1", "md5") if s.get(k)), None)
        cmds = s.get("commands") or []
        out.append(ManifestSource(
            kind=_s("type") or "", url=_s("url"), tag=_s("tag"), commit=_s("commit"), branch=_s("branch"),
            checksum=checksum, commands=tuple(str(c) for c in cmds) if isinstance(cmds, list) else (),
            dest_filename=_s("dest-filename"),
        ))
    return out


def parse_manifest_text(text: str, filename: str | None = None) -> Manifest:
    data = _load(text, filename)
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a mapping")
    data = _json_safe(data)

    app_id = data.get("app-id") or data.get("id")
    if not app_id or not isinstance(app_id, str):
        raise ManifestError("manifest has no app-id")
    if not APP_ID_RE.match(app_id):
        raise ManifestError(f"'{app_id}' is not a reverse-DNS app id")

    modules = _iter_modules(data.get("modules"))
    sources: list[ManifestSource] = []
    for m in modules:
        sources.extend(_parse_sources(m))

    finish_args = data.get("finish-args") or []
    if not isinstance(finish_args, list):
        raise ManifestError("finish-args must be a list")

    return Manifest(
        app_id=app_id,
        runtime=data.get("runtime"),
        runtime_version=str(data["runtime-version"]) if "runtime-version" in data else None,
        sdk=data.get("sdk"),
        command=data.get("command"),
        branch=data.get("branch"),
        finish_args=[str(a) for a in finish_args],
        modules=[str(m.get("name", "")) for m in modules],
        sources=sources,
        raw=data,
    )


def parse_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    return parse_manifest_text(p.read_text(encoding="utf-8"), p.name)


def looks_like_manifest(text: str) -> bool:
    """Cheap pre-filter for crawler hits before doing a full parse."""
    return ("app-id" in text or '"id"' in text) and "runtime" in text and "modules" in text
