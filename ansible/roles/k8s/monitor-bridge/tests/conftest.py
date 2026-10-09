"""Shared pytest fixtures for the monitor-bridge files/ test suite."""

import pytest

from bridge.config import load_config
from bridge.streaks import State
from _bridge_env import bridge_env


@pytest.fixture
def cfg():
    """The configuration a check runs under, built from the env the pod gets.

    `_bridge_env.bridge_env()` renders `templates/env-secret.yaml.j2`, so every field holds the
    deployed value: `bridge/config*.py` carries no second default for a rendered key (#3659). A
    test that needs a different value narrows this with `dataclasses.replace(cfg, X=...)`, and
    one that is about the READ itself — a malformed number, a derived field, a `_FILE`-mounted
    secret — calls `load_config(bridge_env(X=...))` with the environment it means.

    A `monkeypatch.setattr(bridge.config, "X", ...)` mutates a process-wide global for the
    duration of one test; a fixture hands the code under test the object it reads, so two
    tests can state different configurations without either seeing the other's.
    """
    cfg = load_config(bridge_env())
    assert not cfg.CONFIG_PROBLEMS, cfg.CONFIG_PROBLEMS
    return cfg


@pytest.fixture
def state():
    """A zeroed `bridge.streaks.State`, for a test whose cycles each build their own fake.

    The live `Sources` carries one `State` for the life of the process. A helper that builds a
    fresh `FakeSources` per cycle hands this one to each of them, so the streak a test means to
    advance across cycles actually advances instead of restarting at 0 every call.
    """
    return State()


@pytest.fixture
def seq():
    """Factory for a callable yielding each value on successive calls, like mock side_effect.

    A fixture rather than an importable function: suites answer a FakeSources query this way
    (`get_json=seq(...)`), and `from conftest import seq` resolves to whichever conftest.py
    sys.path reached first once the whole repo suite runs. pytest resolves a fixture by directory, so it cannot collide.

    conftest.py lives in `tests/`, a sibling of `files/` rather than a member of it, so a shared
    test helper here can never be a candidate for the ConfigMap ship list (`monitor_bridge_modules`)
    in the first place.
    """

    def _seq(*values):
        it = iter(values)
        return lambda *a, **k: next(it)

    return _seq
