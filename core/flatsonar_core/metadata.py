"""Parse a deployed Flatpak ``metadata`` keyfile (the one at the root of an app
commit) back into ``finish-args`` so the same risk scorer can run on what was
actually downloaded, not on what an index claims."""

from __future__ import annotations

import configparser


def metadata_to_finish_args(text: str) -> list[str]:
    cp = configparser.ConfigParser(interpolation=None, allow_no_value=True, strict=False, delimiters=("=",))
    cp.optionxform = str  # keys are case-sensitive D-Bus names / env vars
    try:
        cp.read_string(text)
    except configparser.Error:
        return []

    out: list[str] = []

    def _list(section: str, key: str) -> list[str]:
        raw = cp.get(section, key, fallback="") or ""
        return [v for v in raw.split(";") if v]

    if cp.has_section("Context"):
        for kind, key in (("share", "shared"), ("socket", "sockets"), ("device", "devices"),
                          ("filesystem", "filesystems"), ("allow", "features")):
            for v in _list("Context", key):
                if v.startswith("!"):
                    out.append(f"--no{kind}={v[1:]}")
                else:
                    out.append(f"--{kind}={v}")
        for v in _list("Context", "persistent"):
            out.append(f"--persist={v}")

    for section, prefix in (("Session Bus Policy", ""), ("System Bus Policy", "system-")):
        if cp.has_section(section):
            for name, policy in cp.items(section):
                if policy == "talk":
                    out.append(f"--{prefix}talk-name={name}")
                elif policy == "own":
                    out.append(f"--{prefix}own-name={name}")
                elif policy == "none":
                    out.append(f"--no-talk-name={name}")

    if cp.has_section("Environment"):
        for k, v in cp.items("Environment"):
            out.append(f"--env={k}={v or ''}")

    return out


def metadata_app_id(text: str) -> str | None:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    return cp.get("Application", "name", fallback=None) or cp.get("Runtime", "name", fallback=None)
