"""Turn ``flatpak install/update -y`` output into a status line and a progress fraction.

Without a terminal, flatpak prints its transaction table once and then one line per
progress tick (tab-separated table, non-breaking spaces inside sizes)::

     1.        org.gnome.TwentyFortyEight.Locale  stable  i  flathub  < 196.4 kB (partial)
     2.        org.gnome.TwentyFortyEight         stable  i  flathub  < 1.0 MB
    Installing 1/2…
    Installing 1/2… ████▌                25%  6.9 MB/s  00:12

The table's download sizes weight the steps, so a 400 MB runtime followed by a 2 MB
app does not jump to 50% the moment the runtime is done.
"""

from __future__ import annotations

import re

_ROW = re.compile(r"^\s*(\d+)\.$")
_SIZE = re.compile(r"([\d.,]+)\s*(bytes|[kMGT]B)\b")
_TICK = re.compile(r"^(Installing|Updating|Uninstalling)\s+(\d+)/(\d+)(?:…|\.\.\.)?(.*)$")
_PERCENT = re.compile(r"(\d{1,3})%")
_SPEED = re.compile(r"([\d.,]+\s*(?:bytes|[kMGT]B)/s)")
_ETA = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_UNIT = {"bytes": 1.0, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def _bytes(text: str) -> float | None:
    m = _SIZE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")) * _UNIT[m.group(2)]
    except ValueError:
        return None


class FlatpakProgress:
    """Feed it flatpak's output line by line; :meth:`feed` returns ``(text, fraction)``
    for progress ticks and ``None`` for everything else (the table, the permission
    summary, "Looking for matches…")."""

    def __init__(self, app_id: str, app_name: str, verb: str = "Downloading"):
        self.app_id = app_id
        self.app_name = app_name
        self.verb = verb
        self.items: dict[int, tuple[str, str]] = {}  # step -> (id, branch)
        self.sizes: dict[int, float] = {}
        self._best: dict[int, int] = {}  # step -> highest percent seen; flatpak's own figure wobbles

    def _label(self, step: int) -> str:
        ref_id, branch = self.items.get(step, ("", ""))
        if not ref_id or ref_id == self.app_id:
            return self.app_name
        if ref_id == f"{self.app_id}.Locale":
            return f"{self.app_name} translations"
        if ref_id.endswith(".Locale"):
            return "translations"
        return f"{ref_id} {branch}".strip()

    def fraction(self, step: int, steps: int, done: float) -> float:
        """Overall progress: finished steps plus ``done`` (0..1) of the current one."""
        sizes = [self.sizes.get(i) for i in range(1, steps + 1)]
        if all(s is not None for s in sizes) and sum(sizes) > 0:
            total = float(sum(sizes))
            return min(1.0, (sum(sizes[: step - 1]) + sizes[step - 1] * done) / total)
        return min(1.0, ((step - 1) + done) / max(steps, 1))

    def feed(self, line: str) -> tuple[str, float] | None:
        line = line.replace("\xa0", " ").rstrip()
        cols = line.split("\t")
        if len(cols) >= 7 and _ROW.match(cols[0]):
            step = int(_ROW.match(cols[0]).group(1))
            self.items[step] = (cols[2].strip(), cols[3].strip())
            size = _bytes(cols[-1])
            if size is not None:
                self.sizes[step] = size
            return None
        m = _TICK.match(line.strip())
        if not m:
            return None
        step, steps, rest = int(m.group(2)), int(m.group(3)), m.group(4)
        m_pct = _PERCENT.search(rest)
        pct = max(self._best.get(step, 0), min(100, int(m_pct.group(1)))) if m_pct else None
        if pct is not None:
            self._best[step] = pct
        bits = [f"{self.verb} {self._label(step)}"]
        if steps > 1:
            bits.append(f"{step} of {steps}")
        if pct is not None:
            bits.append(f"{pct}%")
        speed = _SPEED.search(rest)
        if speed and not speed.group(1).startswith("0 "):
            bits.append(speed.group(1))
        eta = _ETA.search(rest)
        if eta:
            bits.append(f"{eta.group(1)} left")
        return " · ".join(bits), self.fraction(step, steps, (pct or 0) / 100)
