"""Is anyone still tending this app?

Flathub's curation implicitly vouches for that: a listing had to be submitted and
built by somebody recently enough to pass review. Flatsonar indexes straight from
source control instead, so a listing might be a thriving project or a five-year-old
fork nobody ever came back to, and nothing in the crawl said which. This module
makes that visible instead of silent, the same shape as publisher trust (a level
plus the findings behind it): a stale or abandoned app is not removed or blocked,
just labelled honestly, same as an unverified publisher.

Pure: no network, no database. The crawler is the only caller today, because the
signals (repository archived flag, last-push date) only exist server-side; there is
nothing here for the client to re-derive at install time the way it re-audits a
manifest for trust.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone

from .risk import Finding, RiskLevel


class MaintenanceLevel(enum.IntEnum):
    """How likely an app still has someone behind it. Higher is better."""

    ABANDONED = 0  # archived by its owner, or no commits in years
    STALE = 1  # quiet for a while, not (yet) written off
    ACTIVE = 2  # recent activity, or Flathub vouches for it

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> "MaintenanceLevel":
        return cls[label.upper()]


# Past this many days since the last push, a project is "quiet" rather than
# straightforwardly active; past the second, quiet becomes "written off" even
# without an explicit archive. Generous on purpose: plenty of small, finished
# utilities go years between commits because they simply do not need any -
# these thresholds are meant to catch true abandonment, not a stable tool.
STALE_AFTER_DAYS = 730  # ~2 years
ABANDONED_AFTER_DAYS = 1460  # ~4 years


@dataclass(frozen=True)
class MaintenanceResult:
    level: MaintenanceLevel
    findings: list[Finding]


def assess_maintenance(
    *, on_flathub: bool, archived: bool, pushed_at: datetime | None, now: datetime | None = None,
) -> MaintenanceResult:
    """``on_flathub`` apps are always ``active``: Flathub's own review and automatic
    updates are already a maintenance signal, and Flatsonar has no upstream-repository
    activity for most of them (the crawler only ever reads Flathub's packaging
    metadata, not the app's own repository) - nothing here to hold against them.
    """
    if on_flathub:
        return MaintenanceResult(MaintenanceLevel.ACTIVE, [])

    now = now or datetime.now(timezone.utc)
    if archived:
        return MaintenanceResult(MaintenanceLevel.ABANDONED, [
            Finding("maintenance:archived", RiskLevel.YELLOW,
                    "the upstream repository has been archived by its owner"),
        ])
    if pushed_at is None:
        # Nothing to go on (a bundle or remote with no repository metadata attached).
        # Absence of a signal is not evidence of neglect.
        return MaintenanceResult(MaintenanceLevel.ACTIVE, [])

    age_days = (now - pushed_at).days
    if age_days >= ABANDONED_AFTER_DAYS:
        return MaintenanceResult(MaintenanceLevel.ABANDONED, [
            Finding("maintenance:stale", RiskLevel.YELLOW,
                    f"no commits in over {age_days // 365} years"),
        ])
    if age_days >= STALE_AFTER_DAYS:
        years = age_days / 365
        when = f"{years:.0f} years" if years >= 1.5 else "a year or more"
        return MaintenanceResult(MaintenanceLevel.STALE, [
            Finding("maintenance:stale", RiskLevel.YELLOW, f"no commits in {when}"),
        ])
    return MaintenanceResult(MaintenanceLevel.ACTIVE, [])
