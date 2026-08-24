"""Shared pytest fixtures for the monitor-bridge check.py test suite."""

import check
import pytest


@pytest.fixture(autouse=True)
def _reset_down_streaks():
    """Zero check.py's consecutive-down-streak state before every test.

    `_down_streaks` (check_ups/check_ha_heartbeat/check_discord/check_longhorn_volumes)
    accumulates across calls, so a streak left over from one test used to leak into the
    next test's first call — every test that cared used to open with its own
    `check._down_streaks["x"] = 0` line. This fixture replaces those ~27 hand-written
    resets with one reset applied to every test.
    """
    check._down_streaks.clear()
