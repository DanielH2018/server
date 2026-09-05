"""Shared pytest fixtures for the monitor-bridge files/ test suite."""

import bridge.streaks
import pytest

from bridge.config import load_config


@pytest.fixture
def cfg():
    """The configuration a check runs under, built from an EMPTY environment.

    Every field therefore holds the documented default in `bridge/config.py`, which is what a
    test wants unless it says otherwise. A test that needs a different value narrows this with
    `dataclasses.replace(cfg, X=...)`, and one that is about the READ itself — a malformed
    number, a derived field, a `_FILE`-mounted secret — calls `load_config({...})` directly
    with the environment it means.

    This replaces the 118 `monkeypatch.setattr(bridge.config, "X", ...)` sites the suite
    carried until 2026-09-04. A patch mutates a process-wide global for the duration of one
    test; a fixture hands the code under test the object it reads, so two tests can state
    different configurations without either seeing the other's.
    """
    return load_config({})


@pytest.fixture(autouse=True)
def _reset_down_streaks():
    """Zero check.py's consecutive-down-streak state before every test.

    `_down_streaks` (check_ups/check_ha_heartbeat/check_discord/check_longhorn_volumes)
    accumulates across calls, so a streak left over from one test used to leak into the
    next test's first call — every test that cared used to open with its own
    `check._down_streaks["x"] = 0` line. This fixture replaces those ~27 hand-written
    resets with one reset applied to every test.
    """
    bridge.streaks._down_streaks.clear()


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
