"""Shared fixtures for the deploy-tools tests.

Keeps `land.sh`'s landing annotations out of the real syslog (see `_no_syslog`), and holds
the two fixtures every land_lib phase test drives: a Landing for one phase, and `land_run`
for the whole pipeline through land.main. The fakes they build on live in _land_fakes.py.

Run: uv run pytest scripts/deploy_tools/tests -k land
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from _land_fakes import PRIMARY, Fakes, build_tools, make_landing

# The stub records one line per call so a test can assert it intercepted something. Without
# that record the fixture would be indistinguishable from one that silently stopped being on
# PATH, which is the failure mode `test_land_annotation_is_intercepted` exists to catch.
_LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$LAND_TEST_LOGGER_CALLS"
"""


@pytest.fixture(autouse=True)
def _no_syslog(tmp_path_factory, monkeypatch):
    """Put a fake `logger` on PATH so a test run writes nothing to syslog.

    A landing emits one logfmt line through `logger` on every verdict, the `die` paths
    included. Every test module here that runs the real script against a stubbed `gh` reached
    the host's syslog that way, shipped to Loki, and landed on the Landings dashboard beside
    real landings.

    Measured over the two days to 2026-09-03: 2,169 of the 2,577 landing annotations in Loki
    were fixtures — 84% of the board. They are not merely extra rows. They carry `pr=999`,
    `pr=939` and `pr=unknown` with `verdict=aborted`, so any group-by over the dashboard
    reports a landing failure rate dominated by tests that passed.

    Autouse and directory-wide rather than opt-in per test: the modules that run `land.sh`
    today are not a closed set, and one added later would silently start polluting again. A
    stubbed `logger` no test calls costs nothing.

    Those modules build their subprocess env from `os.environ` with their own stub dir
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
def land_run(capsys, monkeypatch):
    """Run `land.main(argv)` against Fakes; (rc, stdout, stderr, calls, logline).

    `primary` is overridden because `Options` defaults it to the real primary checkout,
    which exists on the deploy host and not in CI -- and the pipeline now refuses a primary
    that is not a directory. Pass `primary=` to drive that refusal.
    """
    import land

    def run(argv: list[str], fakes: Fakes | None = None, primary: Path = PRIMARY):
        tools, calls = build_tools(fakes or Fakes())
        if "--pr" not in argv:
            argv = [*argv, "--pr", "999"]
        real_parse = land.parse_args
        monkeypatch.setattr(
            land,
            "parse_args",
            lambda a, d: replace(real_parse(a, d), primary=primary),
        )
        rc = land.main(argv, tools=tools)
        cap = capsys.readouterr()
        logline = next((c[1][0] for c in calls if c[0] == "logger"), "")
        return rc, cap.out, cap.err, calls, logline

    return run


@pytest.fixture(autouse=True)
def _no_syspath_leak():
    """Fail a test that leaves a new directory on `sys.path`.

    `host_lib`, `deploy_logic` and `bridge.common` are cross-role modules imported by BARE
    NAME, and they resolve through pytest's `pythonpath` rather than through a sibling file.
    A directory inserted at index 0 and left there therefore shadows them for every later
    test in the process. That is not hypothetical: `test_notify_never_raises_on_a_broken_host_lib`
    left a tmp_path holding a `host_lib.py` whose only line is `raise RuntimeError('boom')`,
    and `test_tick_ledger_report.py` -- a module the change never touched -- failed on it.

    Membership rather than list equality: re-inserting a path that is already there adds no
    new import candidate, and several modules here do that at import time.

    Directory-scoped rather than repo-wide on purpose. This is where the leak class was
    observed and where the bare-name imports concentrate; a repo-wide assertion would police
    suites that have never had the problem.
    """
    before = set(sys.path)
    yield
    added = [p for p in sys.path if p not in before]
    assert not added, (
        f"this test left {added} on sys.path; a later test importing a cross-role module "
        "by bare name would resolve against it. Remove the entry in a finally, or use "
        "monkeypatch.syspath_prepend."
    )
