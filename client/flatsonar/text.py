"""Wording and small text helpers shared by the pages. No GTK here, so it is testable
anywhere. Labels and tooltips follow the website's
(server/flatsonar_server/web/routes.py), in the client's own voice ("you'll be warned")."""

from __future__ import annotations

import re
from datetime import datetime

RISK_TEXT = {"green": "Sandboxed", "yellow": "Broad permissions", "red": "Extensive permissions"}
RISK_TIP = {
    "green": "Only ordinary sandbox permissions.",
    "yellow": "Asks for permissions that weaken the sandbox. You'll be warned before installing.",
    "red": "Asks for permissions that reach well outside the sandbox: full filesystem or bus access, "
           "for example. Plenty of legitimate apps need this. Nobody has reviewed whether this one "
           "actually does, so you'll be warned twice.",
}

# Publisher trust, from the server's provenance checks. Never let unverified look verified.
TRUST_TEXT = {"verified": "Verified creator", "reviewed": "Flathub reviewed", "unverified": "Unverified publisher",
              "suspicious": "Suspicious publisher"}
TRUST_TIP = {
    "verified": "The creator demonstrably controls this app id: Flathub verification, the hosting account owns "
                "the namespace, or a well-known file on their domain.",
    "reviewed": "On Flathub: the manifest was reviewed and built on Flathub's infrastructure, but the developer "
                "has not verified ownership of the app id.",
    "unverified": "Nobody has confirmed that the publisher controls this app id. You'll be warned before installing.",
    "suspicious": "Something concrete is wrong: the id claims a namespace this repository does not own, or the "
                  "build does things a build should not. You'll be warned twice.",
}

MAINTENANCE_TIP = {
    "active": "Recent activity on the upstream repository, or built and reviewed by Flathub.",
    "stale": "No commits in a year or more. Might still work fine; nobody has touched it in a while.",
    "abandoned": "The upstream repository is archived, or has had no commits in several years.",
}

FUNDING_LABEL = {
    "github": "GitHub Sponsors", "patreon": "Patreon", "ko_fi": "Ko-fi", "liberapay": "Liberapay",
    "open_collective": "Open Collective", "buy_me_a_coffee": "Buy Me a Coffee", "custom": "Donate",
}

# What each install source means, as the website's install panel puts it.
SOURCE_TEXT = {
    "flathub": ("From Flathub", "Built and reviewed by Flathub; updates come from Flathub."),
    "remote": ("From the project's own remote", "Adds a third-party remote: future updates of this app come from it."),
    "bundle": ("From a release bundle",
               "A single .flatpak file attached to a release. Bundles do not update themselves."),
    "manifest": ("Built from source", "Only a flatpak-builder manifest exists; the build runs on your machine."),
}


def funding_label(link: dict[str, str]) -> str:
    """Several links on one platform (FUNDING.yml's ``github:`` takes up to four
    usernames) get their handle, so they don't read as identical duplicate buttons."""
    platform = link.get("platform", "")
    base = FUNDING_LABEL.get(platform, platform.replace("_", " ").title())
    if platform == "custom":
        return base  # an arbitrary donation page, not a per-person handle
    handle = link.get("url", "").rstrip("/").rsplit("/", 1)[-1]
    return f"{base} · {handle}" if handle else base


def paragraphs(text: str | None) -> list[str | list[str]]:
    """Plain text with blank-line paragraphs and ``- `` bullets (what the crawler stores)
    -> paragraphs (str) and bullet lists (list of str). Hard-wrapped lines are joined."""
    out: list[str | list[str]] = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.startswith(("- ", "* ", "• ")) for line in lines):
            out.append([line[2:].strip() for line in lines])
        else:
            out.append(" ".join(lines))
    return out


def nicedate(iso: str | None, fmt: str = "%b %d, %Y") -> str:
    """``2024-03-05T12:00:00Z`` -> ``Mar 5, 2024``; unparseable input comes back as-is."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    return d.strftime(fmt).replace(" 0", " ")


def grouped(n: int) -> str:
    return f"{n:,}"
