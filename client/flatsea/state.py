"""Local per-user state: which warnings the user already accepted.

Once someone has pressed "Sure" twice for a given set of findings, updates of
the same app with the *same* permissions do not nag again. If the permissions
change, the fingerprint changes and the warning comes back.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from flatsea_core import RiskReport

from .paths import data_dir

DATA_DIR = data_dir() / "flatsea"


class Decisions:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "decisions.json"
        self._data: dict[str, dict] = {}
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    @staticmethod
    def fingerprint(report: RiskReport) -> str:
        key = "\n".join(sorted(report.reasons))
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def already_accepted(self, app_id: str, fingerprint: str) -> bool:
        return self._data.get(app_id, {}).get("fingerprint") == fingerprint

    def accept(self, app_id: str, fingerprint: str, report: RiskReport) -> None:
        self._data[app_id] = {
            "fingerprint": fingerprint,
            "level": report.level.label,
            "reasons": report.reasons,
            "accepted_at": int(time.time()),
        }
        self._save()

    def forget(self, app_id: str) -> None:
        if self._data.pop(app_id, None) is not None:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
