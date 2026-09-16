"""Who is allowed to publish under an app id, and does this repository qualify?

Flatpak app ids are reverse-DNS, which makes them a claim of ownership:
``io.github.alice.Foo`` says "alice on GitHub made this", ``org.mozilla.firefox``
says "Mozilla made this". Flathub enforces that claim by hand at review time and
with its verification programme. Flatsonar indexes apps that never went through
either, so it re-derives the rule set here and applies it to wherever the
manifest was actually found.

The outcome is one of four :class:`TrustLevel` values plus a list of findings
explaining it. This module is pure: no network, no database. The server feeds it
the hosting repo and the manifest's build sources; the client can run the same
checks on what it downloaded.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .risk import Finding, RiskLevel


class TrustLevel(enum.IntEnum):
    """How sure we are that the app comes from whoever the id says. Higher is better."""

    SUSPICIOUS = 0  # a concrete red flag: namespace impersonation, dangerous build, ...
    UNVERIFIED = 1  # off-Flathub and ownership could not be confirmed; nothing known against it
    REVIEWED = 2  # on Flathub: manifest reviewed and built on Flathub infrastructure, publisher not verified
    VERIFIED = 3  # the creator demonstrably controls the app id

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> "TrustLevel":
        return cls[label.upper()]


class Ownership(str, enum.Enum):
    VERIFIED = "verified"  # hosting repo owns the namespace
    THIRD_PARTY = "third_party"  # someone else packages the real upstream (builds from the namespace owner)
    UNKNOWN = "unknown"  # custom domain, nothing to compare against
    IMPERSONATION = "impersonation"  # claims a namespace it neither owns nor builds from


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str | None
    path: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.path}" if self.path else f"https://{self.host}"


_SSH_RE = re.compile(r"^(?:git@|ssh://git@)([^:/]+)[:/](.+?)(?:\.git)?/?$")


def repo_ref(url: str | None) -> RepoRef | None:
    """``https://github.com/Alice/foo.git`` -> host ``github.com``, owner ``alice``."""
    if not url:
        return None
    url = url.strip()
    m = _SSH_RE.match(url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    segs = [s for s in parts.path.strip("/").removesuffix(".git").split("/") if s]
    # GitLab web URLs carry "/-/tree/..." after the project path.
    if "-" in segs:
        segs = segs[: segs.index("-")]
    return RepoRef(host=host, owner=segs[0].lower() if segs else None, path="/".join(segs))


# --- namespace rules ------------------------------------------------------------

# Forge namespaces: the third id segment is a user/org on that forge. Flathub's rule;
# dashes in user names become underscores in ids.
_FORGE_NAMESPACES: dict[str, tuple[str, ...]] = {
    "io.github": ("github.com",),
    "com.github": ("github.com",),  # deprecated spelling, still around
    "io.gitlab": ("gitlab.com",),
    "com.gitlab": ("gitlab.com",),
    "io.codeberg": ("codeberg.org",),
    "page.codeberg": ("codeberg.org",),
    "io.frama": ("framagit.org",),
    "io.sourceforge": ("sourceforge.net", "git.code.sf.net"),
    "org.gnome.gitlab": ("gitlab.gnome.org",),
    "io.gitlab.gnome": ("gitlab.gnome.org",),
    "org.freedesktop.gitlab": ("gitlab.freedesktop.org",),
}

# Project / vendor namespaces: anyone hosting under these hosts (optionally owners)
# may publish. Off-Flathub repos elsewhere claiming them are impostors unless they
# build the vendor's own code (third-party packaging).
_VENDOR_NAMESPACES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "org.gnome": (("gitlab.gnome.org", None), ("github.com", "gnome")),
    "org.gimp": (("gitlab.gnome.org", None),),
    "org.kde": (("invent.kde.org", None), ("kde.org", None), ("github.com", "kde")),
    "org.freedesktop": (("gitlab.freedesktop.org", None), ("freedesktop.org", None)),
    "org.xfce": (("gitlab.xfce.org", None), ("xfce.org", None)),
    "io.elementary": (("github.com", "elementary"),),
    "org.mozilla": (("mozilla.org", None), ("github.com", "mozilla"), ("github.com", "mozilla-mobile")),
    "com.google": (("google.com", None), ("googlesource.com", None), ("github.com", "google")),
    "org.chromium": (("googlesource.com", None), ("github.com", "chromium")),
    "com.microsoft": (("microsoft.com", None), ("github.com", "microsoft")),
    "com.visualstudio": (("microsoft.com", None), ("github.com", "microsoft")),
    "com.valvesoftware": (("steampowered.com", None), ("github.com", "valvesoftware")),
    "com.spotify": (("spotify.com", None), ("github.com", "spotify")),
    "com.discordapp": (("discord.com", None), ("discordapp.com", None)),
    "com.slack": (("slack.com", None), ("slack-edge.com", None)),
    "us.zoom": (("zoom.us", None),),
    "org.videolan": (("videolan.org", None), ("github.com", "videolan")),
    "org.libreoffice": (("libreoffice.org", None), ("documentfoundation.org", None), ("github.com", "libreoffice")),
    "org.blender": (("blender.org", None), ("github.com", "blender")),
    "org.telegram": (("telegram.org", None), ("github.com", "telegramdesktop")),
    "org.signal": (("signal.org", None), ("github.com", "signalapp")),
    "com.brave": (("brave.com", None), ("github.com", "brave")),
    "org.torproject": (("torproject.org", None),),
    "org.keepassxc": (("keepassxc.org", None), ("github.com", "keepassxreboot")),
    "com.bitwarden": (("bitwarden.com", None), ("github.com", "bitwarden")),
    "org.inkscape": (("inkscape.org", None), ("gitlab.com", "inkscape")),
    "org.audacityteam": (("audacityteam.org", None), ("github.com", "audacity")),
    "com.obsproject": (("obsproject.com", None), ("github.com", "obsproject")),
    "org.wireshark": (("wireshark.org", None), ("gitlab.com", "wireshark")),
    "org.qbittorrent": (("qbittorrent.org", None), ("github.com", "qbittorrent")),
    "net.lutris": (("lutris.net", None), ("github.com", "lutris")),
    "com.heroicgameslauncher": (("github.com", "heroic-games-launcher"),),
    "org.flathub": (("flathub.org", None), ("github.com", "flathub")),
    "org.flatpak": (("flatpak.org", None), ("github.com", "flatpak")),
    "com.jetbrains": (("jetbrains.com", None), ("github.com", "jetbrains")),
    "org.thunderbird": (("mozilla.org", None), ("thunderbird.net", None)),
    "org.onlyoffice": (("onlyoffice.com", None), ("github.com", "onlyoffice")),
    "com.nextcloud": (("nextcloud.com", None), ("github.com", "nextcloud")),
    "org.zotero": (("zotero.org", None), ("github.com", "zotero")),
    "io.mpv": (("mpv.io", None), ("github.com", "mpv-player")),
    "org.kicad": (("kicad.org", None), ("gitlab.com", "kicad")),
    "org.godotengine": (("godotengine.org", None), ("github.com", "godotengine")),
    "org.prismlauncher": (("prismlauncher.org", None), ("github.com", "prismlauncher")),
    "org.freecad": (("freecad.org", None), ("github.com", "freecad")),
    "org.octave": (("octave.org", None), ("gnu.org", None)),
    "org.gnu": (("gnu.org", None),),
    "org.apache": (("apache.org", None), ("github.com", "apache")),
    "org.eclipse": (("eclipse.org", None), ("github.com", "eclipse")),
    "org.python": (("python.org", None), ("github.com", "python")),
    "org.debian": (("debian.org", None),),
    "org.fedoraproject": (("fedoraproject.org", None), ("pagure.io", None)),
    "com.ubuntu": (("ubuntu.com", None), ("launchpad.net", None)),
    "org.mageia": (("mageia.org", None),),
    "org.opensuse": (("opensuse.org", None), ("github.com", "openSUSE")),
}


def _id_user(segment: str) -> str:
    return segment.lower().replace("_", "-")


def _host_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith("." + allowed)


def domain_of(app_id: str) -> str | None:
    """``com.example.team.Foo`` -> ``team.example.com`` (what a well-known file would live on).

    Forge namespaces have no meaningful domain to check and return None.
    """
    segs = app_id.split(".")
    if len(segs) < 3:
        return None
    for ns in _FORGE_NAMESPACES:
        if app_id.lower().startswith(ns + "."):
            return None
    domain = ".".join(reversed(segs[:-1])).lower()
    return domain if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain) else None


@dataclass
class OwnershipResult:
    status: Ownership
    reason: str
    namespace: str | None = None  # the rule that matched, e.g. "io.github" or "org.mozilla"
    expected_owner: str | None = None  # for forge namespaces: the user the id names
    builds_from: list[str] = field(default_factory=list)


def _namespace_owner_ok(app_id: str, ref: RepoRef | None) -> tuple[str | None, str | None, bool | None]:
    """Returns (namespace, expected owner, ok). ok is None when no rule applies."""
    lowered = app_id.lower()
    segs = app_id.split(".")
    # Longest matching forge namespace first (org.gnome.gitlab before org.gnome).
    for ns in sorted(_FORGE_NAMESPACES, key=len, reverse=True):
        if lowered.startswith(ns + ".") and len(segs) > ns.count(".") + 2:
            user = segs[ns.count(".") + 1]
            hosts = _FORGE_NAMESPACES[ns]
            ok = bool(ref and ref.owner and any(_host_matches(ref.host, h) for h in hosts)
                      and _id_user(ref.owner) == _id_user(user))
            return ns, user, ok
    for ns in sorted(_VENDOR_NAMESPACES, key=len, reverse=True):
        if lowered.startswith(ns + "."):
            ok = bool(ref and any(
                _host_matches(ref.host, host) and (owner is None or (ref.owner or "") == owner.lower())
                for host, owner in _VENDOR_NAMESPACES[ns]
            ))
            return ns, None, ok
    return None, None, None


def check_ownership(app_id: str, hosted_at: str | None, builds_from: list[str] | None = None) -> OwnershipResult:
    """Does the repository at ``hosted_at`` own the namespace of ``app_id``?

    ``builds_from`` are the manifest's source URLs (most-likely-upstream first). A
    repo that does not own the namespace but builds the namespace owner's code is a
    third-party packager, not an impostor.
    """
    ref = repo_ref(hosted_at)
    ns, user, ok = _namespace_owner_ok(app_id, ref)
    where = f"{ref.host}/{ref.owner}" if ref and ref.owner else (ref.host if ref else "an unknown location")
    builds = [u for u in (builds_from or []) if u]

    if ns is None:
        domain = domain_of(app_id)
        return OwnershipResult(
            Ownership.UNKNOWN,
            f"{app_id} claims the domain {domain}; only its owner can prove that" if domain
            else f"{app_id} is not under a namespace Flatsonar can check",
            builds_from=builds,
        )
    if ok:
        return OwnershipResult(Ownership.VERIFIED, f"hosted by {where}, which owns the {ns} namespace of {app_id}",
                               namespace=ns, expected_owner=user, builds_from=builds)

    # Not the owner. Does it at least build the owner's code?
    for src in builds:
        _, _, src_ok = _namespace_owner_ok(app_id, repo_ref(src))
        if src_ok:
            return OwnershipResult(
                Ownership.THIRD_PARTY,
                f"packaged by {where}, not by the {ns} namespace owner; it does build the owner's code from {src}",
                namespace=ns, expected_owner=user, builds_from=builds,
            )
    owner_text = f"{ns}.{user}" if user else ns
    return OwnershipResult(
        Ownership.IMPERSONATION,
        f"{app_id} claims the {owner_text} namespace but is hosted by {where} and does not build the owner's code",
        namespace=ns, expected_owner=user, builds_from=builds,
    )


def ownership_finding(result: OwnershipResult) -> Finding:
    level = {
        Ownership.VERIFIED: RiskLevel.GREEN,
        Ownership.THIRD_PARTY: RiskLevel.YELLOW,
        Ownership.UNKNOWN: RiskLevel.YELLOW,
        Ownership.IMPERSONATION: RiskLevel.RED,
    }[result.status]
    return Finding("publisher:namespace", level, result.reason)


def trust_from_findings(base: TrustLevel, findings: list[Finding]) -> TrustLevel:
    """Any red finding makes an app suspicious whatever else we know about it."""
    if any(f.level == RiskLevel.RED for f in findings):
        return TrustLevel.SUSPICIOUS
    return base


def same_repo(a: str | None, b: str | None) -> bool:
    ra, rb = repo_ref(a), repo_ref(b)
    if not ra or not rb:
        return False
    return (ra.host, ra.path.lower()) == (rb.host, rb.path.lower())
