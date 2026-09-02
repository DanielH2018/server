"""Shared pytest fixtures for the monitor-bridge check.py test suite."""

import bridge_streaks
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
    bridge_streaks._down_streaks.clear()


@pytest.fixture
def seq():
    """Factory for a callable yielding each value on successive calls, like mock side_effect.

    A fixture rather than an importable function: three suites stub `check._get_json` this way,
    and `from conftest import seq` resolves to whichever conftest.py sys.path reached first once
    the whole repo suite runs. pytest resolves a fixture by directory, so it cannot collide.

    conftest.py lives in `tests/`, a sibling of `files/` rather than a member of it, so a shared
    test helper here can never be a candidate for the ConfigMap ship list (`monitor_bridge_modules`)
    in the first place.
    """

    def _seq(*values):
        it = iter(values)
        return lambda *a, **k: next(it)

    return _seq
