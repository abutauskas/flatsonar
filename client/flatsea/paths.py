"""XDG-ish directories that also behave on Windows/MSYS2 (useful for previewing the UI)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    for var in ("HOME", "USERPROFILE"):
        if os.environ.get(var):
            return Path(os.environ[var])
    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local/share")


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or home() / ".cache")
