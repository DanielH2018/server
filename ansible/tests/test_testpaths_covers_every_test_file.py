"""Guard: every tracked pytest file lies under some `testpaths` entry in pyproject.toml.

`testpaths` is hand-enumerated — 21 entries covering `ansible/tests`, `scripts`,
`.claude/hooks`, `evals` and a dozen per-role `files/` directories. Nothing derives it, so a
role that ships tests in a `files/` directory nobody added falls outside it silently. The tests
are written, reviewed and committed; they pass when invoked directly; and `uv run pytest` never
collects them. There is no error to read, because the suite reports the count it always
reported.

This is the residual gap stated in `cef07465`, the commit that deleted
`test_prek_pytest_files_cover_testpaths.py`: "this covers tests under testpaths only". That
commit settled the dispatch question for the prek hook itself — `always_run = true`, no `files`
gate, pinned by `test_prek_pytest_always_runs.py` — and left open what `testpaths` reaches.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED:
a guard that matches everything and a guard that matches nothing are indistinguishable from the
passing side alone.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import PurePosixPath

from _helpers import REPO

# pytest's default `python_files`. This repo sets no override, and every tracked test uses the
# `test_*.py` form — the `*_test.py` form matches nothing today. Widen this the day one appears.
_TEST_FILE_GLOB = "test_*.py"


def _testpaths() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    paths = data["tool"]["pytest"]["ini_options"]["testpaths"]
    assert paths, "pyproject.toml declares no testpaths"
    return paths


def _tracked_test_files() -> list[str]:
    """Every pytest file in THIS commit.

    DECIDED: `git ls-files`, not `rglob` — the same reason `_helpers.discover_docs` gives. This
    repo grows a full working tree per live session under `.claude/worktrees/<name>/`, holding
    older copies of these same files; an rglob would judge this commit against other sessions'
    checkouts and fail on paths that moved legitimately.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        rel
        for rel in listed.split("\0")
        if rel and PurePosixPath(rel).match(_TEST_FILE_GLOB)
    )


def orphaned_test_files(test_files, testpaths) -> list[str]:
    """The test files lying under no `testpaths` entry, and so never collected.

    Matching is by path component rather than string prefix: a `scripts` entry must not be read
    as covering `scripts_extra/test_x.py`. `is_relative_to` gives that for free, where a
    `str.startswith` would silently pass the exact file this guard exists to catch.
    """
    roots = [PurePosixPath(p) for p in testpaths]
    return [
        path
        for path in test_files
        if not any(PurePosixPath(path).is_relative_to(root) for root in roots)
    ]


def test_repo_has_no_orphaned_test_files() -> None:
    orphans = orphaned_test_files(_tracked_test_files(), _testpaths())
    assert not orphans, (
        "these test files lie under no `testpaths` entry, so `uv run pytest` never collects "
        f"them and they cannot fail: {orphans}. Add the containing directory to `testpaths` in "
        "pyproject.toml, or move the tests under one already listed."
    )


def test_a_test_file_outside_testpaths_is_flagged() -> None:
    # The RED proof for the assertion above: on a clean tree it passes whether the matching
    # works or has silently stopped matching anything at all.
    orphans = orphaned_test_files(
        ["ansible/tests/test_a.py", "ansible/roles/k8s/newthing/files/test_b.py"],
        ["ansible/tests"],
    )
    assert orphans == ["ansible/roles/k8s/newthing/files/test_b.py"]


def test_a_sibling_sharing_a_name_prefix_is_flagged() -> None:
    # `scripts` must not be read as covering `scripts_extra/`. A `str.startswith`
    # implementation passes every other test in this file and fails only this one.
    assert orphaned_test_files(["scripts_extra/test_a.py"], ["scripts"]) == [
        "scripts_extra/test_a.py"
    ]


def test_a_test_file_under_a_testpath_is_clean() -> None:
    assert orphaned_test_files(["scripts/dev/test_a.py"], ["scripts"]) == []
