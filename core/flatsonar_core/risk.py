"""Score Flatpak sandbox permissions (``finish-args``) into green / yellow / red.

The rules mirror what Flathub's own "potentially unsafe" badge looks at, plus a
few extras (LD_PRELOAD, docker socket, autostart dirs, wildcard bus names).
Every argument gets a *finding* with a human-readable description so the UI
can render permission badges; the report level is the worst finding.

Green findings are informational: they describe a permission but do not, on
their own, trigger the double-confirm flow in the client.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Iterable


class RiskLevel(enum.IntEnum):
    GREEN = 0
    YELLOW = 1
    RED = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> "RiskLevel":
        return cls[label.upper()]


@dataclass(frozen=True)
class Finding:
    arg: str
    level: RiskLevel
    reason: str

    def __str__(self) -> str:
        return f"[{self.level.label}] {self.arg}: {self.reason}"


@dataclass
class RiskReport:
    level: RiskLevel
    findings: list[Finding] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """Only the non-green findings, as strings - what the warning dialog lists."""
        return [str(f) for f in self.findings if f.level > RiskLevel.GREEN]

    @property
    def is_risky(self) -> bool:
        return self.level > RiskLevel.GREEN

    def escalate(self, level: RiskLevel, arg: str, reason: str) -> "RiskReport":
        """Add an external finding (e.g. a ClamAV hit) and bump the level if needed."""
        self.findings.append(Finding(arg, level, reason))
        if level > self.level:
            self.level = level
        return self

    def extend(self, findings: Iterable[Finding]) -> "RiskReport":
        """Merge findings from another check (manifest audit, publisher trust)."""
        for f in findings:
            self.escalate(f.level, f.arg, f.reason)
        return self


# --- filesystem -------------------------------------------------------------

_CREDENTIAL_PATHS = (
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.password-store",
    "xdg-config/autostart",
    "~/.config/autostart",
    "~/.bashrc",
    "~/.bash_profile",
    "~/.profile",
    "~/.zshrc",
    "xdg-run/docker.sock",
    "/run/docker.sock",
    "/var/run/docker.sock",
)

_WHOLE_XDG_DIRS = {"xdg-config", "xdg-data", "xdg-cache"}
_USER_MEDIA_DIRS = {
    "xdg-download",
    "xdg-documents",
    "xdg-pictures",
    "xdg-music",
    "xdg-videos",
    "xdg-desktop",
    "xdg-public-share",
    "xdg-templates",
}


def _filesystem(value: str) -> tuple[RiskLevel, str]:
    path, _, mode = value.partition(":")
    ro = mode == "ro"
    norm = path.rstrip("/") or "/"

    if norm in ("host", "/"):
        if ro:
            return RiskLevel.YELLOW, "read-only access to your entire file system"
        return RiskLevel.RED, "full access to your entire file system"
    if norm in ("host-os", "host-etc", "/usr", "/etc"):
        return RiskLevel.YELLOW, "access to operating-system files"
    if norm in ("home", "~", "/home"):
        if ro:
            return RiskLevel.YELLOW, "read-only access to your home folder"
        return RiskLevel.YELLOW, "full access to your home folder"
    for cred in _CREDENTIAL_PATHS:
        if norm == cred or norm.startswith(cred + "/"):
            return RiskLevel.RED, f"access to a sensitive location ({cred}): credentials or autostart"
    if "docker.sock" in norm or "podman.sock" in norm:
        return RiskLevel.RED, "access to a container socket (equivalent to root)"
    if norm in _WHOLE_XDG_DIRS:
        return RiskLevel.YELLOW, f"access to your whole {norm[4:]} directory (other apps' settings and data)"
    if norm in _USER_MEDIA_DIRS:
        return RiskLevel.GREEN, f"access to your {norm[4:]} folder"
    if norm.startswith(("xdg-run/", "xdg-config/", "xdg-data/", "xdg-cache/")):
        return RiskLevel.GREEN, f"access to {norm}"
    if norm in ("/tmp", "/media", "/mnt", "/run/media", "/var/tmp", "/srv", "/opt"):
        return RiskLevel.GREEN, f"access to {norm}"
    if norm.startswith(("~/", "/")):
        return RiskLevel.GREEN, f"access to {norm}"
    return RiskLevel.YELLOW, f"access to unrecognised path {value!r}"


# --- sockets / devices / sharing ---------------------------------------------

_SOCKETS: dict[str, tuple[RiskLevel, str]] = {
    "wayland": (RiskLevel.GREEN, "Wayland display"),
    "fallback-x11": (RiskLevel.GREEN, "X11 display, only when Wayland is unavailable"),
    "x11": (RiskLevel.YELLOW, "X11 display: X11 lets apps read input and windows of other apps"),
    "pulseaudio": (RiskLevel.GREEN, "audio"),
    "system-bus": (RiskLevel.RED, "unrestricted access to the system D-Bus (system services, hardware)"),
    "session-bus": (RiskLevel.RED, "unrestricted access to the session D-Bus (all your running apps)"),
    "ssh-auth": (RiskLevel.YELLOW, "your SSH agent (can sign with your SSH keys)"),
    "gpg-agent": (RiskLevel.YELLOW, "your GPG agent (can sign/decrypt with your keys)"),
    "pcsc": (RiskLevel.YELLOW, "smart card readers"),
    "cups": (RiskLevel.GREEN, "printing"),
    "inherit-wayland-socket": (RiskLevel.GREEN, "inherited Wayland socket"),
}

_DEVICES: dict[str, tuple[RiskLevel, str]] = {
    "all": (RiskLevel.RED, "all devices (/dev): webcams, disks, raw hardware"),
    "dri": (RiskLevel.GREEN, "GPU acceleration"),
    "kvm": (RiskLevel.YELLOW, "virtualisation (KVM)"),
    "shm": (RiskLevel.GREEN, "shared memory"),
    "input": (RiskLevel.YELLOW, "raw input devices (keyboards, mice, gamepads)"),
    "usb": (RiskLevel.YELLOW, "USB devices"),
}

_SHARES: dict[str, tuple[RiskLevel, str]] = {
    "network": (RiskLevel.GREEN, "network"),
    "ipc": (RiskLevel.GREEN, "inter-process communication (needed for X11)"),
}

_ALLOWS: dict[str, tuple[RiskLevel, str]] = {
    "devel": (RiskLevel.YELLOW, "development features (ptrace, can inspect other processes in its sandbox)"),
    "multiarch": (RiskLevel.GREEN, "multi-architecture binaries"),
    "bluetooth": (RiskLevel.GREEN, "Bluetooth"),
    "canbus": (RiskLevel.YELLOW, "CAN bus sockets"),
    "per-app-dev-shm": (RiskLevel.GREEN, "per-app shared memory"),
}

# --- D-Bus names --------------------------------------------------------------

_TALK_NAMES: dict[str, tuple[RiskLevel, str]] = {
    "org.freedesktop.Flatpak": (RiskLevel.RED, "sandbox escape: can run commands on the host via flatpak-spawn"),
    "org.freedesktop.Flatpak.Development": (RiskLevel.RED, "sandbox escape: flatpak development interface"),
    "org.freedesktop.impl.portal.PermissionStore": (RiskLevel.YELLOW, "can change portal permissions"),
    "org.freedesktop.secrets": (RiskLevel.YELLOW, "your keyring / saved passwords"),
    "org.freedesktop.systemd1": (RiskLevel.YELLOW, "systemd: can manage services"),
    "org.freedesktop.login1": (RiskLevel.YELLOW, "session management (logout, suspend)"),
    "org.freedesktop.NetworkManager": (RiskLevel.YELLOW, "network configuration"),
    "org.freedesktop.PackageKit": (RiskLevel.YELLOW, "package management"),
    "org.freedesktop.PolicyKit1": (RiskLevel.YELLOW, "authorisation (polkit)"),
    "org.freedesktop.UPower": (RiskLevel.GREEN, "battery status"),
    "org.freedesktop.Notifications": (RiskLevel.GREEN, "notifications"),
    "org.freedesktop.ScreenSaver": (RiskLevel.GREEN, "screensaver inhibit"),
    "org.gnome.SessionManager": (RiskLevel.GREEN, "session inhibit"),
    "org.gnome.Shell": (RiskLevel.YELLOW, "GNOME Shell: screenshots, keybindings"),
    "org.gnome.Mutter.DisplayConfig": (RiskLevel.GREEN, "display configuration"),
    "org.kde.StatusNotifierWatcher": (RiskLevel.GREEN, "system tray"),
    "org.a11y.Bus": (RiskLevel.GREEN, "accessibility"),
    "ca.desrt.dconf": (RiskLevel.YELLOW, "dconf: all GNOME settings"),
}

_BROAD_WILDCARDS = {"*", "org.*", "org.freedesktop.*", "com.*", "net.*", "io.*", "de.*"}
_WILDCARD = re.compile(r"\.\*$|[*?]")


def _bus_name(kind: str, name: str) -> tuple[RiskLevel, str]:
    if name in _BROAD_WILDCARDS:
        return RiskLevel.RED, f"wildcard {kind} name: can reach almost any service"
    if name in _TALK_NAMES:
        return _TALK_NAMES[name]
    if name.startswith("org.freedesktop.portal."):
        return RiskLevel.GREEN, f"portal {name.rsplit('.', 1)[-1]}"
    if name.startswith("org.mpris.MediaPlayer2"):
        return RiskLevel.GREEN, "media player controls (MPRIS)"
    if kind == "system-talk":
        return RiskLevel.YELLOW, f"talks to system service {name}"
    return RiskLevel.GREEN, f"talks to {name}"


def _own_name(name: str, app_id: str | None) -> tuple[RiskLevel, str]:
    if app_id and (name == app_id or name.startswith(app_id + ".")):
        return RiskLevel.GREEN, "owns its own bus name"
    if name.startswith("org.mpris.MediaPlayer2."):
        return RiskLevel.GREEN, "registers as a media player (MPRIS)"
    if name in _BROAD_WILDCARDS or (_WILDCARD.search(name) and name.count(".") <= 2):
        return RiskLevel.RED, f"wildcard own-name {name}: can impersonate system services"
    if name.startswith("org.freedesktop.") and not name.startswith("org.freedesktop.portal."):
        return RiskLevel.RED, f"owns freedesktop name {name}: can impersonate a system service"
    if name.startswith(("org.kde.", "org.gnome.")):
        return RiskLevel.YELLOW, f"owns desktop name {name}: can impersonate a desktop component"
    return RiskLevel.GREEN, f"owns bus name {name}"


# --- environment / misc -------------------------------------------------------

_ENV_RED = {"LD_PRELOAD", "LD_AUDIT"}
_ENV_YELLOW = {"LD_LIBRARY_PATH", "PATH", "GTK_MODULES", "QT_PLUGIN_PATH", "PYTHONPATH", "GIO_EXTRA_MODULES"}


def _env(value: str) -> tuple[RiskLevel, str]:
    key, _, _ = value.partition("=")
    if key in _ENV_RED:
        return RiskLevel.RED, f"sets {key}: injects code into every process in the sandbox"
    if key in _ENV_YELLOW:
        return RiskLevel.YELLOW, f"sets {key}: changes which libraries or plugins get loaded"
    return RiskLevel.GREEN, f"sets environment variable {key}"


_RESTRICTIVE = {"nofilesystem", "nosocket", "nodevice", "noshare", "unshare", "no-talk-name", "disallow", "nousb",
                "unset-env"}
_NEUTRAL = {"extension", "metadata", "sdk-extension", "runtime-extension", "require-version", "usb",
            "add-policy", "remove-policy"}


# --- entry point --------------------------------------------------------------


def score_arg(arg: str, app_id: str | None = None) -> Finding:
    if not arg.startswith("--"):
        return Finding(arg, RiskLevel.YELLOW, "unrecognised finish-arg")
    key, _, value = arg[2:].partition("=")

    if key == "filesystem":
        level, reason = _filesystem(value)
    elif key == "socket":
        level, reason = _SOCKETS.get(value, (RiskLevel.YELLOW, f"unknown socket {value}"))
    elif key == "device":
        level, reason = _DEVICES.get(value, (RiskLevel.YELLOW, f"unknown device {value}"))
    elif key == "share":
        level, reason = _SHARES.get(value, (RiskLevel.YELLOW, f"unknown share {value}"))
    elif key == "allow":
        level, reason = _ALLOWS.get(value, (RiskLevel.YELLOW, f"unknown allow {value}"))
    elif key in ("talk-name", "system-talk-name"):
        level, reason = _bus_name(key.removesuffix("-name"), value)
    elif key in ("own-name", "system-own-name"):
        level, reason = _own_name(value, app_id)
    elif key == "env":
        level, reason = _env(value)
    elif key == "persist":
        if value in (".", ""):
            level, reason = RiskLevel.GREEN, "keeps its whole sandbox home between runs"
        else:
            level, reason = RiskLevel.GREEN, f"keeps {value} between runs"
    elif key in _RESTRICTIVE:
        level, reason = RiskLevel.GREEN, f"restricts {value}".strip()
    elif key in _NEUTRAL:
        level, reason = RiskLevel.GREEN, f"{key} {value}".strip()
    else:
        level, reason = RiskLevel.YELLOW, f"unrecognised finish-arg {key}"

    return Finding(arg, level, reason)


def score_finish_args(finish_args: Iterable[str], app_id: str | None = None) -> RiskReport:
    findings = [score_arg(a, app_id) for a in finish_args]
    level = max((f.level for f in findings), default=RiskLevel.GREEN)
    return RiskReport(level=level, findings=findings)
