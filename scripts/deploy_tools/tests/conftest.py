"""Shared fixtures for the deploy-tools tests.

Keeps `land.sh`'s landing annotations out of the real syslog (see `_no_syslog`), and holds
the two fixtures every land_lib phase test drives: a Landing for one phase, and `land_run`
for the whole pipeline through land.main. The fakes they build on live in _land_fakes.py.

Run: uv run pytest scripts/deploy_tools/tests -k land
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _land_fakes import Fakes, build_tools, make_landing

# The stub records one line per call so a test can assert it intercepted something. Without
# that record the fixture would be indistinguishable from one that silently stopped being on
# PATH, which is the failure mode `test_land_annotation_is_intercepted` exists to catch.
_LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$LAND_TEST_LOGGER_CALLS"
"""


@pytest.fixture(autouse=True)
def _no_syslog(tmp_path_factory, monkeypatch):
    """Put a fake `logger` on PATH so a test run writes nothing to syslog.

    `land.sh` emits one logfmt line per landing through `logger`, from an EXIT trap that fires
    on every verdict including the `die` paths. Three test modules here run the real script
    against a stubbed `gh`, so every one of those runs reached the host's syslog, shipped to
    Loki, and landed on the Landings dashboard beside real landings.

    Measured over the two days to 2026-09-03: 2,169 of the 2,577 landing annotations in Loki
    were fixtures — 84% of the board. They are not merely extra rows. They carry `pr=999`,
    `pr=939` and `pr=unknown` with `verdict=aborted`, so any group-by over the dashboard
    reports a landing failure rate dominated by tests that passed.

    Autouse and directory-wide rather than opt-in per test: the three modules that run
    `land.sh` today are not a closed set, and a fourth added later would silently start
    polluting again. A stubbed `logger` no test calls costs nothing.

    The three modules build their subprocess env from `os.environ` with their own stub dir
    prepended, so mutating PATH here is inherited by all of them: their `gh` stub still wins
    for `gh`, and this wins for `logger` over `/usr/bin/logger`.
    """
    stub_dir = tmp_path_factory.mktemp("logger-stub")
    logger = stub_dir / "logger"
    logger.write_text(_LOGGER_STUB)
    logger.chmod(0o755)

    calls = stub_dir / "logger-calls"
    calls.touch()
    monkeypatch.setenv("LAND_TEST_LOGGER_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


@pytest.fixture
def logger_calls(_no_syslog) -> Path:
    """The file the stubbed `logger` appends to, one line per call."""
    return _no_syslog


@pytest.fixture
def landing():
    return make_landing


@pytest.fixture
def land_run(capsys):
    """Run `land.main(argv)` against Fakes; (rc, stdout, stderr, calls, logline)."""
    # land.py doesn't exist until Task 5; this fixture is unused (and this import unreached)
    # until then.
    import land  # ty: ignore[unresolved-import]

    def run(argv: list[str], fakes: Fakes | None = None):
        tools, calls = build_tools(fakes or Fakes())
        if "--pr" not in argv:
            argv = [*argv, "--pr", "999"]
        rc = land.main(argv, tools=tools)
        cap = capsys.readouterr()
        logline = next((c[1][0] for c in calls if c[0] == "logger"), "")
        return rc, cap.out, cap.err, calls, logline

    return run
