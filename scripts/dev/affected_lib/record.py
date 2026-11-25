"""Run one test module under `strace` and reduce what it touched to a table row.

Each module runs in its own pytest process. A shared process would hide a dependency behind a
cache: the second module to need a parsed file or an imported helper finds it in memory and
reads nothing. Alone, a module pays for every read it depends on, and for the shared render
prebuilds when it is one of their readers.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Reach `scripts/dev` and `scripts/lib`: a directly-invoked script gets only its own directory on sys.path.


from dev.affected_lib import trace, traced_pytest
from dev.affected_lib.table import Row, Table, parent_dirs, tracked_blobs
from lib.git import git

# The docs-refresh cron's `timeout` is its `--budget` plus this.
MODULE_TIMEOUT_SECONDS = 300
_PYTEST_NO_TESTS_COLLECTED = 5
# The import system asks for a compiled module under eight names in every package directory.
_NEVER_TRACKED_SUFFIXES = (".so", ".pyc")
_SKIPPED = re.compile(r"\b\d+ skipped\b")


def _is_utf8(path: str) -> bool:
    """Whether `path` names no undecodable byte, which the JSON table could not hold."""
    try:
        path.encode()
    except UnicodeEncodeError:
        return False
    return True


def reduce(
    touched: trace.Touched,
    harness_listed: set[str],
    repo: Path,
    tracked: frozenset[str],
    git_index: str,
) -> Row:
    """Keep what `touched` says about the tracked tree under `repo`.

    A missing path with a byte that is not UTF-8 is dropped: `json.dumps` writes its
    surrogate as a lone `\\udcXX` escape, which strict JSON parsers reject, and git reports
    no change name such a row could match.

    Args:
        touched: The parsed trace of one module's run.
        harness_listed: Directories only the import system or the collector listed, which
            are dropped from the traced command's own listings.
        repo: The checkout the module ran in.
        tracked: Every tracked path in it.
        git_index: The absolute path of the checkout's git index.
    """
    root = str(repo)
    tracked_dirs = parent_dirs(tracked)

    def relative(path: str) -> str | None:
        if path == root:
            return ""
        return path[len(root) + 1 :] if path.startswith(root + "/") else None

    def in_tracked_dir(rel: str) -> bool:
        """Whether the nearest directory that exists above `rel` is a tracked one."""
        if "__pycache__" in rel.split("/") or rel.endswith(_NEVER_TRACKED_SUFFIXES):
            return False
        while "/" in rel:
            rel = rel.rsplit("/", 1)[0]
            if (repo / rel).is_dir():
                return rel in tracked_dirs
        return True

    listed = set()
    for pid, dirs in touched.listed.items():
        listed |= dirs - harness_listed if pid == touched.root_pid else dirs
    reads = {relative(path) for path in touched.reads}
    missing = {relative(path) for path in touched.missing}
    return Row(
        files=frozenset(tracked.intersection(reads)),
        dirs=frozenset(tracked_dirs.intersection(relative(path) for path in listed)),
        missing=frozenset(
            path
            for path in missing
            if path and path not in tracked and _is_utf8(path) and in_tracked_dir(path)
        ),
        tree=git_index in touched.reads,
    )


def record_module(
    module: str, repo: Path, tracked: frozenset[str], git_index: str
) -> Row | str:
    """Trace `module`'s own pytest run and return its row, or the reason it has none.

    A module whose run fails or times out gets no row, because a failing run stops early and
    reads less than a passing one. A module that skipped a test gets none either: the skipped
    test may run on a CI runner, and what it reads there is in no trace taken here.
    """
    with tempfile.TemporaryDirectory(prefix="affected-") as scratch:
        log, listings = Path(scratch, "strace.log"), Path(scratch, "listings.log")
        command = [
            "strace", "-f", "-y", "-qq", "--seccomp-bpf", "-s", "4096",
            "-e", f"trace={trace.SYSCALLS}", "-e", "signal=none", "-o", str(log),
            sys.executable, traced_pytest.__file__, "-n0", "-q", "-p", "no:cacheprovider",
            module,
        ]  # fmt: skip
        try:
            proc = subprocess.run(
                command,
                cwd=repo,
                env=os.environ | {traced_pytest.LOG_ENV: str(listings)},
                capture_output=True,
                text=True,
                timeout=MODULE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {MODULE_TIMEOUT_SECONDS}s"
        summary = (proc.stdout.strip().splitlines() or [""])[-1]
        if proc.returncode not in (0, _PYTEST_NO_TESTS_COLLECTED):
            return f"pytest exit {proc.returncode}"
        if _SKIPPED.search(summary):
            return "skipped a test on this host"
        with log.open(errors="surrogateescape") as lines:
            touched = trace.parse(lines, str(repo))
        harness = traced_pytest.harness_only(
            listings.read_text() if listings.exists() else ""
        )
    return reduce(touched, harness, repo, tracked, git_index)


def refresh(
    table: Table,
    queue: list[str],
    repo: Path,
    *,
    budget_seconds: float | None = None,
    jobs: int = 4,
    record=record_module,
    clock=time.monotonic,
) -> tuple[list[str], dict[str, str]]:
    """Record the modules of `queue` into `table`, in order, `jobs` at a time.

    A module whose run is refused loses its row, so the selection runs it on every pull
    request until a later run records it.

    Args:
        table: The table to update in place.
        queue: The modules to record, most urgent first.
        repo: The checkout to run in.
        budget_seconds: Start no module after this many seconds; None records all of `queue`.
        jobs: How many modules to trace at once.
        record: Traces one module, as `record_module` does.
        clock: Reads elapsed seconds.

    Returns:
        The modules recorded, and for each refused module the reason it has no row.
    """
    blobs = tracked_blobs(repo)
    tracked = frozenset(blobs)
    git_index = (
        git("rev-parse", "--absolute-git-dir", cwd=repo).stdout.strip() + "/index"
    )
    deadline = None if budget_seconds is None else clock() + budget_seconds

    def one(module: str) -> Row | str | None:
        if deadline is not None and clock() >= deadline:
            return None
        return record(module, repo, tracked, git_index)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, queue))
    recorded, refused = [], {}
    for module, result in zip(queue, results, strict=True):
        if isinstance(result, Row):
            table.put(module, result, blobs[module])
            recorded.append(module)
        elif result is not None:
            refused[module] = result
            table.drop(module)
    return recorded, refused
