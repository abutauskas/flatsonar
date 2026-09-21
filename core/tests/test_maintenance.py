"""The maintenance signal: is anyone still tending this app?"""

from datetime import datetime, timedelta, timezone

from flatsonar_core import MaintenanceLevel, RiskLevel, assess_maintenance

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def test_flathub_apps_are_always_active():
    # Even an archived, ancient upstream: Flathub's own review is the signal here.
    result = assess_maintenance(on_flathub=True, archived=True, pushed_at=_days_ago(5000), now=NOW)
    assert result.level is MaintenanceLevel.ACTIVE and result.findings == []


def test_recent_activity_is_active():
    result = assess_maintenance(on_flathub=False, archived=False, pushed_at=_days_ago(10), now=NOW)
    assert result.level is MaintenanceLevel.ACTIVE and result.findings == []


def test_no_pushed_at_is_not_held_against_it():
    """Absence of a signal (a bundle/remote candidate with no repo metadata) is not
    evidence of neglect."""
    result = assess_maintenance(on_flathub=False, archived=False, pushed_at=None, now=NOW)
    assert result.level is MaintenanceLevel.ACTIVE and result.findings == []


def test_quiet_for_a_year_or_more_is_stale():
    result = assess_maintenance(on_flathub=False, archived=False, pushed_at=_days_ago(800), now=NOW)
    assert result.level is MaintenanceLevel.STALE
    assert result.findings[0].level is RiskLevel.YELLOW
    assert result.findings[0].arg == "maintenance:stale"


def test_quiet_for_years_is_abandoned():
    result = assess_maintenance(on_flathub=False, archived=False, pushed_at=_days_ago(2000), now=NOW)
    assert result.level is MaintenanceLevel.ABANDONED
    assert "years" in result.findings[0].reason


def test_archived_is_always_abandoned_regardless_of_last_push():
    # Archived the day it last saw a commit: still abandoned, not "active".
    result = assess_maintenance(on_flathub=False, archived=True, pushed_at=_days_ago(1), now=NOW)
    assert result.level is MaintenanceLevel.ABANDONED
    assert result.findings[0].arg == "maintenance:archived"


def test_level_ordering():
    assert MaintenanceLevel.ABANDONED < MaintenanceLevel.STALE < MaintenanceLevel.ACTIVE


def test_label_round_trip():
    for level in MaintenanceLevel:
        assert MaintenanceLevel.from_label(level.label) is level
