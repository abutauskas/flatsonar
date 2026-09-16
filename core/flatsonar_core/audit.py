"""Static audit of a flatpak-builder manifest: what does *building and installing*
this thing do, before we look at the sandbox it will run in?

Runtime permissions are :mod:`risk`'s job. This module looks at the other half:
where the bytes come from (pinned? encrypted? built from source?), what the build
sandbox is allowed to touch, and whether any build command does something a
build has no business doing (``curl | sh``, base64-decoding blobs, setuid bits).

Everything here matters most for apps Flatsonar builds locally from a manifest,
where the build runs on the user's machine. It is still worth showing for Flathub
apps: ``extra-data`` is downloaded at install time no matter who built the app.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from .manifest import Manifest, ManifestSource
from .risk import Finding, RiskLevel, score_arg

# --- commands ------------------------------------------------------------------

# (pattern, level, reason), worst first. Checked against every build-command,
# post-install, cleanup-command and shell/script source line.
_COMMAND_RULES: tuple[tuple[re.Pattern[str], RiskLevel, str], ...] = (
    (re.compile(r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b"), RiskLevel.RED,
     "pipes a download straight into a shell"),
    (re.compile(r"\bbase64\s+(-d|--decode)\b|\bbase64\b[^\n]*\|\s*(ba)?sh\b"), RiskLevel.RED,
     "decodes an embedded blob (hidden payload)"),
    (re.compile(r"\bxxd\s+-r\b|\bopenssl\s+enc\b[^\n]*\s-d\b"), RiskLevel.RED,
     "decodes an embedded blob (hidden payload)"),
    (re.compile(r"\beval\s+[\"']?\$\("), RiskLevel.RED, "evaluates generated shell code"),
    (re.compile(r"/dev/tcp/|\b(nc|ncat|netcat)\b[^\n]*\s-e\b|\bsocat\b"), RiskLevel.RED,
     "opens a raw network connection from the build"),
    (re.compile(r"\bchmod\b[^\n]*\b([ug]\+s|[42][0-7]{3})\b|\bsetcap\b"), RiskLevel.RED,
     "sets setuid/setgid or capabilities on a file"),
    (re.compile(r"\bflatpak-spawn\b[^\n]*--host\b"), RiskLevel.RED, "runs commands on the host during the build"),
    (re.compile(r"\b(curl|wget|git\s+clone|pip3?\s+install|npm\s+(install|ci)|cargo\s+fetch|go\s+(get|mod\s+download))\b"),
     RiskLevel.YELLOW, "fetches from the network during the build (nothing to audit beforehand)"),
    (re.compile(r"\bsudo\b|\bsu\s+-\b"), RiskLevel.YELLOW, "tries to escalate privileges"),
    (re.compile(r"\b(python3?|perl|ruby|node)\s+-[ce]\s+[\"'][^\"'\n]{120,}"), RiskLevel.YELLOW,
     "runs a long inline script"),
)

_CMD_KEYS = ("build-commands", "post-install", "cleanup-commands", "prepare-commands", "test-commands")
# The network rule only means something when the build sandbox has network at all.
_NETWORK_RULE = "fetches from the network"
_OFFLINE_FLAGS = ("--no-index", "--offline", "--frozen", "-mod=vendor", "--find-links=file:")

# build-args that let the build reach outside its sandbox. Their level comes from the
# same table as finish-args, bumped to at least yellow: a build should need none of them.
_ESCAPE_PREFIXES = ("--filesystem=", "--socket=", "--device=", "--talk-name=", "--system-talk-name=",
                    "--own-name=", "--system-own-name=", "--env=LD_PRELOAD", "--env=LD_AUDIT")

# --- sources -------------------------------------------------------------------

_BINARY_EXT = (".deb", ".rpm", ".appimage", ".snap", ".exe", ".msi", ".dmg", ".apk", ".jar", ".run", ".bin")
_BINARY_ARCH = re.compile(r"(linux|lin)[-_]?(x86[-_]?64|amd64|x64|aarch64|arm64|i[36]86)"
                          r"|[-_.](x86[-_]?64|amd64|aarch64|arm64)[-_.]", re.IGNORECASE)


def _url_bits(url: str) -> tuple[str, str, str]:
    p = urlsplit(url)
    return (p.scheme or "").lower(), (p.hostname or "").lower(), posixpath.basename(p.path or "")


def _looks_prebuilt(src: ManifestSource) -> bool:
    if src.kind not in ("archive", "file", "extra-data") or not src.url:
        return False
    name = (src.dest_filename or _url_bits(src.url)[2]).lower()
    if name.endswith(_BINARY_EXT):
        return True
    # foo-1.2-linux-x86_64.tar.gz is a binary release; foo-1.2-src.tar.gz is not.
    return bool(_BINARY_ARCH.search(name)) and "src" not in name and "source" not in name


def _audit_source(src: ManifestSource, idx: int) -> Iterable[Finding]:
    tag = f"source[{idx}]"
    if src.kind == "extra-data":
        host = _url_bits(src.url)[1] if src.url else "?"
        yield Finding(f"{tag}:extra-data", RiskLevel.YELLOW,
                      f"downloads {host} at install time, after any scan has run")
    if src.url:
        scheme, host, _ = _url_bits(src.url)
        if scheme in ("http", "ftp", "git"):
            yield Finding(f"{tag}:insecure-url", RiskLevel.YELLOW,
                          f"fetches from {host} over unencrypted {scheme}: anyone on the path can swap the bytes")
    if src.kind in ("git", "archive", "file", "extra-data") and not src.pinned:
        what = "tracks a moving branch" if src.kind == "git" else "has no checksum"
        yield Finding(f"{tag}:unpinned", RiskLevel.YELLOW,
                      f"{src.kind} source {what}: what gets built can change without the manifest changing")
    if _looks_prebuilt(src):
        yield Finding(f"{tag}:prebuilt", RiskLevel.YELLOW, "installs a prebuilt binary instead of building from source")
    for line in src.commands:
        yield from _audit_command(line, f"{tag}:command")


def _audit_command(cmd: str, tag: str, network: bool = True) -> Iterable[Finding]:
    """``network`` is whether the build sandbox can reach the network. Without it a
    ``pip install`` either uses vendored wheels (``--no-index``) or fails; either way
    there is nothing to warn about."""
    for pat, level, reason in _COMMAND_RULES:
        if not pat.search(cmd):
            continue
        if reason.startswith(_NETWORK_RULE) and (not network or any(f in cmd for f in _OFFLINE_FLAGS)):
            continue
        snippet = " ".join(cmd.split())
        yield Finding(tag, level, f"{reason}: {snippet[:120]}")
        return  # one finding per command; rules are ordered worst first


def _has_network(opts: Any) -> bool:
    return isinstance(opts, dict) and "--share=network" in [str(a) for a in (opts.get("build-args") or [])]


def _audit_build_args(args: Any, tag: str) -> Iterable[Finding]:
    if not isinstance(args, list):
        return
    for arg in map(str, args):
        if arg == "--share=network":
            yield Finding(f"{tag}:{arg}", RiskLevel.YELLOW,
                          "the build has network access: it can download things the manifest does not list")
            continue
        f = score_arg(arg)
        if arg.startswith(_ESCAPE_PREFIXES):
            yield Finding(f"{tag}:{arg}", max(f.level, RiskLevel.YELLOW), f"opens the build sandbox: {f.reason}")
        elif f.level > RiskLevel.GREEN:
            yield Finding(f"{tag}:{arg}", f.level, f"build sandbox: {f.reason}")


def _audit_build_options(opts: Any, tag: str) -> Iterable[Finding]:
    if not isinstance(opts, dict):
        return
    yield from _audit_build_args(opts.get("build-args"), tag)
    yield from _audit_build_args(opts.get("test-args"), tag)
    env = opts.get("env")
    if isinstance(env, dict):
        for k in env:
            if str(k) in ("LD_PRELOAD", "LD_AUDIT"):
                yield Finding(f"{tag}:env:{k}", RiskLevel.RED, f"sets {k} for the build: injects code into every build step")


def _iter_modules(modules: Any) -> Iterable[dict[str, Any]]:
    for m in modules or []:
        if isinstance(m, dict):
            yield m
            yield from _iter_modules(m.get("modules"))


def _collapse(findings: Iterable[Finding]) -> list[Finding]:
    """Forty unpinned archives are one problem, not forty."""
    groups: dict[tuple[str, RiskLevel], list[Finding]] = {}
    for f in findings:
        key = (re.sub(r"^source\[\d+\]", "source", f.arg), f.level)
        groups.setdefault(key, []).append(f)
    out = []
    for (arg, level), group in groups.items():
        reason = group[0].reason if len(group) == 1 else f"{group[0].reason} (x{len(group)})"
        out.append(Finding(arg, level, reason))
    return out


def audit_manifest(m: Manifest) -> list[Finding]:
    """Findings about fetching and building this manifest. Empty list = nothing to say."""
    out: list[Finding] = []
    for i, src in enumerate(m.sources):
        out.extend(_audit_source(src, i))
    raw = m.raw or {}
    out.extend(_audit_build_options(raw.get("build-options"), "build-options"))
    top_network = _has_network(raw.get("build-options"))
    for mod in _iter_modules(raw.get("modules")):
        name = str(mod.get("name") or "?")
        out.extend(_audit_build_options(mod.get("build-options"), f"{name}:build-options"))
        network = top_network or _has_network(mod.get("build-options"))
        for key in _CMD_KEYS:
            cmds = mod.get(key)
            if isinstance(cmds, list):
                for c in cmds:
                    out.extend(_audit_command(str(c), f"{name}:{key}", network))
    if isinstance(raw.get("cleanup-commands"), list):
        for c in raw["cleanup-commands"]:
            out.extend(_audit_command(str(c), "cleanup-commands", top_network))
    return _collapse(out)


def audit_level(findings: Iterable[Finding]) -> RiskLevel:
    return max((f.level for f in findings), default=RiskLevel.GREEN)
