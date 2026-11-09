"""Report where a run's wall time goes outside every test's timed phases.

Registered as a plugin from `addopts` in pyproject.toml, beside `xdist_width` and
`render_share`. It reports only on a run that passes `--durations`, as every CI pytest leg does.

## Why

A CI leg's wall exceeded its `--durations` total divided by its four workers by 40-120s on the
master runs of 2026-11-08 and 2026-11-09 (#7710). `--durations` times only a test's setup, call
and teardown. Startup, collection, the render prebuilds that `conftest.py` and `render_share`
run in `pytest_collection_finish`, and the idle tail `--dist loadscope` leaves on a worker whose
modules ran out early all fall outside it. The leg's log cannot show them either: the CI step
pipes pytest through `tee`, so stdout is block-buffered and its timestamps are flush times.

## What it prints

One line per process that ran tests: each xdist worker, or `main` without xdist. Every time is
seconds since the controller's session start, read from the monotonic clock, which every
process on one host shares.

- `collected`: collection finished, before the prebuilds.
- `prebuilt`: the `pytest_collection_finish` hooks returned, so the prebuilds are done.
- `tests`: the seconds of setup, call and teardown the process ran, which `--durations` sums.
- `finished`: the process's last test ended.

`down` is when the controller saw the worker shut down. After the workers, the slowest module
collections: import time plus parametrize time, the larger of what any worker measured, since
under `--dist loadscope` every worker collects the whole selection.

Run: uv run pytest ansible/tests/repo/test_phase_timing.py
"""

import time

import pytest

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
        self.collections: dict[str, float] = {}
        self._starts: dict[str, float] = {}

    def as_dict(self) -> dict:
        return {
            "collected": self.collected,
            "prebuilt": self.prebuilt,
            "finished": self.finished,
            "tests": self.tests,
            "collections": self.collections,
        }


_timing = _Timing()
_session_start = 0.0
_workers: dict[str, dict] = {}


def _reporting(config: pytest.Config) -> bool:
    return config.getoption("durations", None) is not None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session_start
    _session_start = time.monotonic()


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
        lines.append(
            f"{name:>5}  collected {_since(start, t['collected'])}"
            f"  prebuilt {_since(start, t['prebuilt'])}"
            f"  tests {t['tests']:7.1f}s  finished {_since(start, t['finished'])}{down}"
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
