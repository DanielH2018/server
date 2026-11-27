"""Report where a run's wall time goes outside every test's timed phases.

Registered as a plugin from `addopts` in pyproject.toml, beside `xdist_width` and
`render_share`. It reports only on a run that passes `--durations`, as every CI pytest leg does.

## Why

A CI leg's wall exceeded its `--durations` total divided by its four workers by 40-120s on the
master runs of 2026-11-08 and 2026-11-09 (#7710). `--durations` times only a test's setup, call
and teardown. Startup, collection, the render prebuilds that `render_share` runs
in `pytest_collection_finish`, and the idle tail `--dist loadscope` leaves on a worker whose
modules ran out early all fall outside it. The leg's log cannot show them either: the CI step
pipes pytest through `tee`, so stdout is block-buffered and its timestamps are flush times.

## What reads it

- The render wall-second constants in `scripts/dev/shard_prebuild.py` come from the `prebuilt`
  column of a master run.
- The `rewrote` column counts the modules a CI leg's restored rewrite cache missed.

## What it prints

One line per process that ran tests: each xdist worker, or `main` without xdist. Every time is
seconds since the controller's session start, read from the monotonic clock, which every
process on one host shares.

- `collected`: collection finished, before the prebuilds.
- `prebuilt`: the `pytest_collection_finish` hooks returned, so the prebuilds are done.
- `tests`: the seconds of setup, call and teardown the process ran, which `--durations` sums.
- `finished`: the process's last test ended.
- `rewrote`: the test modules whose assertions the process rewrote, and the seconds that took.
  pytest rewrites a module only when `__pycache__` holds no rewritten copy whose source hash
  matches, so on a CI leg this counts the modules the restored rewrite cache missed (#8693).
  It reads `-` on a pytest that has no `_rewrite_test` to time.

`down` is when the controller saw the worker shut down. After the workers, the slowest module
collections: import time plus parametrize time, the larger of what any worker measured, since
under `--dist loadscope` every worker collects the whole selection.

Run: uv run pytest tests/repo/suite/test_phase_timing.py
"""

import time
import types
from pathlib import Path

import pytest
from _pytest.assertion import rewrite as _rewrite

KEY = "phase_timing"
# Module collections slower than this are listed, at most `TOP_COLLECTIONS` of them.
COLLECTION_FLOOR_SECONDS = 0.2
TOP_COLLECTIONS = 15


class _Timing:
    def __init__(self) -> None:
        self.collected = 0.0
        self.prebuilt = 0.0
        self.finished = 0.0
        self.tests = 0.0
        self.rewritten: int | None = None
        self.rewrite_seconds = 0.0
        self.collections: dict[str, float] = {}
        self._starts: dict[str, float] = {}

    def as_dict(self) -> dict:
        return {
            "collected": self.collected,
            "prebuilt": self.prebuilt,
            "finished": self.finished,
            "tests": self.tests,
            "rewritten": self.rewritten,
            "rewrite_seconds": self.rewrite_seconds,
            "collections": self.collections,
        }


_timing = _Timing()
_session_start = 0.0
_workers: dict[str, dict] = {}


def _reporting(config: pytest.Config) -> bool:
    return config.getoption("durations", None) is not None


def _time_rewrites() -> None:
    """Wrap pytest's assertion rewrite so each call adds to `_timing`, once per process."""
    original = getattr(_rewrite, "_rewrite_test", None)
    if original is None or getattr(original, KEY, False):
        return

    def timed(fn: Path, config: pytest.Config) -> tuple[bytes, types.CodeType]:
        started = time.perf_counter()
        try:
            return original(fn, config)
        finally:
            _timing.rewritten = (_timing.rewritten or 0) + 1
            _timing.rewrite_seconds += time.perf_counter() - started

    setattr(timed, KEY, True)
    _timing.rewritten = 0
    _rewrite.__dict__["_rewrite_test"] = timed


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session_start
    _session_start = time.monotonic()
    _time_rewrites()


def pytest_collectstart(collector: pytest.Collector) -> None:
    if isinstance(collector, pytest.Module):
        _timing._starts[collector.nodeid] = time.perf_counter()


def pytest_collectreport(report: pytest.CollectReport) -> None:
    started = _timing._starts.pop(report.nodeid, None)
    if started is not None:
        _timing.collections[report.nodeid] = time.perf_counter() - started


@pytest.hookimpl(wrapper=True)
def pytest_collection_finish(session: pytest.Session):
    _timing.collected = time.monotonic()
    try:
        return (yield)
    finally:
        _timing.prebuilt = time.monotonic()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _timing.tests += report.duration


def pytest_runtest_logfinish() -> None:
    _timing.finished = time.monotonic()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[KEY] = _timing.as_dict()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    timing = dict(getattr(node, "workeroutput", {}).get(KEY) or {})
    if timing:
        timing["down"] = time.monotonic()
        _workers[node.gateway.id] = timing


def _since(start: float, at: float) -> str:
    return f"{at - start:7.1f}s" if at else "      -"


def summary_lines(start: float, workers: dict[str, dict]) -> list[str]:
    """The report's lines for `workers`, each a timing dict, relative to `start`."""
    lines = []
    collections: dict[str, float] = {}
    for name, t in sorted(workers.items()):
        down = f"  down {_since(start, t['down'])}" if "down" in t else ""
        rewrote = (
            "-"
            if t.get("rewritten") is None
            else f"{t['rewritten']} in {t['rewrite_seconds']:.1f}s"
        )
        lines.append(
            f"{name:>5}  collected {_since(start, t['collected'])}"
            f"  prebuilt {_since(start, t['prebuilt'])}"
            f"  tests {t['tests']:7.1f}s  finished {_since(start, t['finished'])}"
            f"  rewrote {rewrote}{down}"
        )
        for module, seconds in t["collections"].items():
            collections[module] = max(seconds, collections.get(module, 0.0))
    slow = sorted(
        ((s, m) for m, s in collections.items() if s >= COLLECTION_FLOOR_SECONDS),
        reverse=True,
    )[:TOP_COLLECTIONS]
    if slow:
        lines.append("slowest module collections (import and parametrize):")
        lines += [f"  collect {seconds:6.2f}s  {module}" for seconds, module in slow]
    return lines


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    if not _reporting(config) or hasattr(config, "workeroutput"):
        return
    workers = _workers or {"main": _timing.as_dict()}
    terminalreporter.write_sep("=", "phase timing (seconds since session start)")
    for line in summary_lines(_session_start, workers):
        terminalreporter.write_line(line)
