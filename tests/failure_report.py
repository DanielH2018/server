"""Keep a failed run's tracebacks and short summary at the end of its output, and report them to CI.

Registered as a plugin from `addopts` in pyproject.toml, beside `phase_timing`.

## Why

pytest prints its FAILURES and ERRORS sections first in the terminal summary. The other plugins'
summaries then follow, and the short test summary closes the output. On a CI shard those plugin
summaries are long. One `--durations=0` report ran to 156 KB, and the determinism modules print
a `render digest at` line for each role. All of that landed between the traceback and the short
summary, and the CI log showed no annotation naming the failed test.

## What it does

- On every run, it defers the FAILURES and ERRORS sections. They print just before the short
  test summary, after every other plugin's summary, so the tail of the output is the failure.
- With `--github-report`, which CI's pytest step passes, the controller also does two things.
  It prints one `::error` workflow command for each failed or errored test, naming the file and
  line of the crash, or the test's own location when the crash lies outside the repo. It also
  writes a count line, such as `2 failed, 1210 passed`, at the top of the step's job summary,
  followed by the failed test ids. The flag is explicit because a test that runs pytest in a
  subprocess inherits CI's environment, and an environment check would let that inner run's
  deliberate failures reach the real job summary.

Run: uv run pytest tests/repo/suite/test_failure_report.py
"""

import os
from pathlib import Path

import pytest
from lib.progress import count_line

# The terminal reporter's stats keys a count line names, in the order it names them.
COUNTED = ("failed", "error", "passed", "skipped", "xfailed", "xpassed")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--github-report",
        action="store_true",
        help="print a ::error annotation per failed test and lead the job summary with a count",
    )


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _failed(stats: dict) -> list:
    """The first failing report of each test or collector, in the order they were reported."""
    seen: dict[str, object] = {}
    for report in [*stats.get("failed", []), *stats.get("error", [])]:
        seen.setdefault(report.nodeid, report)
    return list(seen.values())


def _where(report, root: Path) -> tuple[str, int]:
    """The repo-relative file and 1-based line a failure annotation points at."""
    crash = getattr(report.longrepr, "reprcrash", None)
    if crash is not None:
        try:
            return Path(crash.path).resolve().relative_to(root).as_posix(), crash.lineno
        except ValueError:
            pass
    location = getattr(report, "location", None)
    if location:
        return location[0], (location[1] or 0) + 1
    return report.nodeid.split("::")[0], 1


def _first_line(report) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash is not None else str(report.longrepr or "failed")
    return next((line for line in text.splitlines() if line.strip()), "failed")


def annotation(report, root: Path) -> str:
    """The `::error` workflow command for one failed report."""
    path, line = _where(report, root)
    return (
        f"::error file={_escape_property(path)},line={line},"
        f"title={_escape_property(report.nodeid)}::{_escape_data(_first_line(report))}"
    )


def summary_text(stats: dict) -> str:
    """The job summary's head: one count line, then each failed test id and its first line."""
    states = [key for key in COUNTED for _ in stats.get(key, [])]
    lines = [f"**pytest: {count_line(states)}**", ""]
    lines += [f"- `{r.nodeid}`: {_first_line(r)}" for r in _failed(stats)]
    return "\n".join(lines) + "\n\n"


def _lead_summary(text: str) -> None:
    """Put `text` at the top of the step's job summary, above what the tests appended."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    path = Path(summary)
    earlier = path.read_text() if path.exists() else ""
    path.write_text(text + earlier)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter, config: pytest.Config):
    if hasattr(config, "workeroutput"):
        return (yield)
    if config.getoption("github_report"):
        root = config.rootpath.resolve()
        for report in _failed(terminalreporter.stats):
            terminalreporter.write_line(annotation(report, root))
        _lead_summary(summary_text(terminalreporter.stats))
    errors = terminalreporter.summary_errors
    failures = terminalreporter.summary_failures
    short = terminalreporter.short_test_summary

    def failures_then_short_summary() -> None:
        errors()
        failures()
        short()

    terminalreporter.summary_errors = lambda: None
    terminalreporter.summary_failures = lambda: None
    terminalreporter.short_test_summary = failures_then_short_summary
    try:
        return (yield)
    finally:
        del terminalreporter.summary_errors
        del terminalreporter.summary_failures
        del terminalreporter.short_test_summary
