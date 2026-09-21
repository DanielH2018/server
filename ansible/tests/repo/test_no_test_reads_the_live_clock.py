"""No test builds a fixture from the live clock.

A test that stamps its fixture `time.time() - age` and hands it to code that reads
`time.time()` itself asserts a distance measured across the suite's own runtime. The
distance is usually fine and occasionally straddles a threshold — an exact day count
crossing midnight, a "fresh" marker read after a slow collection, a `1.0 days ago` message
that rounds to `1.1`. Every function these tests exercise takes a `now` (or a `clock`), so
the deterministic form is a fixed epoch per module handed to both sides (#2158).

A subprocess-driven test is no exception. The Longhorn reader and reaper suites run their
entry points as real subprocesses and used to stay on the live clock, because handing a
subprocess a `now` looked like an env var the production script would have to honour. Both
entry points now take `main(..., now=None)`, and the harnesses run them through a one-line
`runpy` shim that calls `main` with a fixed epoch — still a subprocess, still the real env
parsing, bootstrap and kubectl shell-out, but no seam the cron's environment can reach
(#2220). `SUBPROCESS_DRIVEN` below is the list of files still excused on that ground; it is
empty, and the guard fails if a listed file stops reading the clock, so it can only shrink.

Run: uv run pytest ansible/tests/repo/test_no_test_reads_the_live_clock.py
"""

import ast
import subprocess
from pathlib import Path

from _helpers import REPO, is_test_file

# A subprocess-driven test whose `now` cannot cross the process boundary. Empty since #2220;
# an entry here needs a reason the `runpy` shim in `_reap_entrypoint_harness.py` does not
# cover, and is removed again the moment the file sheds its clock read.
SUBPROCESS_DRIVEN: frozenset[str] = frozenset()

# Non-vacuity floor: files the census must reach, so a moved `tests/` directory or a renamed
# module cannot empty it and let the guard pass over nothing. The census is every tracked
# module under a `tests/` directory, not just `test_*.py`: a `_*_fixtures.py` helper that
# stamps `time.time() - age` reproduces the defect behind every test that imports it.
KNOWN_TEST_FILES = frozenset(
    {
        "scripts/diagnostics/tests/_alert_fixtures.py",
        "scripts/dev/tests/test_findings_claim_staleness.py",
        "ansible/roles/k8s/monitor-bridge/tests/test_check_r2.py",
        "ansible/roles/setup/k3s/tests/test_longhorn_backup_health_reader.py",
    }
)

# (attribute called, name of the object it is called on). `datetime.datetime.now` and
# `_dt.datetime.now` both end in `datetime.now`, so the owner is the LAST attribute or the
# bare name — never the import alias.
_CLOCK_CALLS = frozenset(
    {
        ("time", "time"),
        ("now", "datetime"),
        ("utcnow", "datetime"),
        ("today", "date"),
        ("today", "datetime"),
    }
)


def _owner_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def clock_reads(source: str) -> list[int]:
    """Line numbers of every `time.time()`, `datetime.now()`, `utcnow()` or `today()` call."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = _owner_name(node.func.value)
        if owner and (node.func.attr, owner) in _CLOCK_CALLS:
            found.append(node.lineno)
    return sorted(found)


def _tracked_test_files() -> list[str]:
    # `git ls-files`, not `rglob`, for the reason `_helpers.discover_docs` records: a
    # root-anchored walk descends into `.claude/worktrees/<name>/` and judges this commit
    # against other sessions' checkouts.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(rel for rel in listed.split("\0") if rel and is_test_file(Path(rel)))


def test_no_test_file_reads_the_live_clock():
    files = _tracked_test_files()
    missing = KNOWN_TEST_FILES - set(files)
    assert not missing, (
        f"test-file census lost {sorted(missing)}; it may now be vacuous"
    )

    failures = []
    still_listed = set()
    for rel in files:
        lines = clock_reads((REPO / rel).read_text(encoding="utf-8"))
        if not lines:
            continue
        if rel in SUBPROCESS_DRIVEN:
            still_listed.add(rel)
            continue
        failures += [f"{rel}:{ln}" for ln in lines]

    shed = SUBPROCESS_DRIVEN - still_listed
    assert not shed, (
        f"{sorted(shed)} no longer read the clock; drop them from SUBPROCESS_DRIVEN"
    )
    assert not failures, (
        "A test fixture dated against the live clock asserts a distance measured across the "
        "suite's own runtime. Pin a module-level epoch and pass it as `now=` (or `clock=`) "
        "to the code under test — every callee here takes one:\n" + "\n".join(failures)
    )


# ---- red proof: the predicate is exercised on fixture sources, one accepting, one rejecting

LIVE_CLOCK = """
import time
import datetime as _dt
from datetime import datetime
stamp = time.time() - 60
a = datetime.now()
b = _dt.datetime.now(tz=_dt.UTC)
c = _dt.date.today()
"""

FIXED_EPOCH = """
from datetime import datetime, UTC
NOW = 1_780_000_000.0
stamp = NOW - 60
when = datetime(2026, 9, 21, tzinfo=UTC)
age = time_of(NOW)  # a call named `time` on something other than the module is not a read
"""


def test_a_live_clock_read_is_flagged():
    assert clock_reads(LIVE_CLOCK) == [5, 6, 7, 8]


def test_a_fixed_epoch_is_clean():
    assert clock_reads(FIXED_EPOCH) == []
