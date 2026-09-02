"""Guards on where a test file lives: under a `testpaths` entry, and in a `tests/` directory.

`testpaths` is hand-enumerated — 21 entries covering `ansible/tests`, `scripts`,
`.claude/hooks`, `evals` and a dozen per-role `tests/` directories. Nothing derives it, so a
role that ships tests in a directory nobody added falls outside it silently. The tests
are written, reviewed and committed; they pass when invoked directly; and `uv run pytest` never
collects them. There is no error to read, because the suite reports the count it always
reported.

The second guard keeps tests out of the directory holding the code they cover. #764 moved 160
of them into sibling `tests/` directories, because a role's `files/` is what the role ships
and a test there was kept off hosts only by per-file copy lists, and because the deployer's
test-only path rule (`deploy_changes._is_test_only_path`) is a directory check. The layout
drifted within the hour that PR was open: master added `scripts/lib/test_invocation_sites.py`
beside its module. `ansible/tests/` satisfies the rule by name. `scripts/conftest.py` is the
one deliberate exception, the shared conftest for the whole `scripts` testpath.

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

# pytest's default `python_files`, both forms. Deriving the notion of "a test file" from a
# hand-kept single glob would reproduce, inside this guard, the enumeration failure it exists
# to catch: a `foo_test.py` outside testpaths would be invisible to pytest AND to this check.
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")


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
        if rel and any(PurePosixPath(rel).match(g) for g in _TEST_FILE_GLOBS)
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


# Test-suite files that may sit outside a `tests/` directory, and why.
_LAYOUT_EXCEPTIONS = {
    # The shared conftest for the `scripts` testpath; pytest finds it by walking up from
    # each collected file, so it has to sit at the root the subdirectories share.
    "scripts/conftest.py",
}


def _tracked_suite_files() -> list[str]:
    """The test files plus every `conftest.py`:

    a conftest beside shipped code is the same hazard as a test beside it, and #764 moved two of
    them.
    """
    return sorted(
        set(_tracked_test_files())
        | {
            rel
            for rel in subprocess.run(
                ["git", "ls-files", "-z", "--", "**/conftest.py", "conftest.py"],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split("\0")
            if rel
        }
    )


def misplaced_suite_files(suite_files, exceptions=()) -> list[str]:
    """The suite files with no `tests` directory anywhere on their path."""
    return [
        path
        for path in suite_files
        if "tests" not in PurePosixPath(path).parts[:-1] and path not in exceptions
    ]


def test_every_suite_file_sits_in_a_tests_directory() -> None:
    misplaced = misplaced_suite_files(_tracked_suite_files(), _LAYOUT_EXCEPTIONS)
    assert not misplaced, (
        "these test files sit beside the code they cover; move each into a sibling `tests/` "
        f"directory (a role's `files/` is what the role ships): {misplaced}"
    )


def test_a_test_beside_its_module_is_flagged() -> None:
    # The RED proof: a test in a role's files/ and one at a scripts subdirectory root.
    assert misplaced_suite_files(
        [
            "ansible/roles/k8s/thing/files/test_a.py",
            "scripts/lib/test_b.py",
            "scripts/lib/tests/test_c.py",
        ]
    ) == ["ansible/roles/k8s/thing/files/test_a.py", "scripts/lib/test_b.py"]


def test_a_conftest_beside_shipped_code_is_flagged() -> None:
    assert misplaced_suite_files(["ansible/roles/k8s/thing/files/conftest.py"]) == [
        "ansible/roles/k8s/thing/files/conftest.py"
    ]


def test_a_file_named_tests_does_not_count_as_a_directory() -> None:
    # Only a directory component satisfies the rule; `parts[:-1]` drops the filename.
    assert misplaced_suite_files(["scripts/lib/tests.py"]) == ["scripts/lib/tests.py"]


def test_the_named_exception_is_clean() -> None:
    assert misplaced_suite_files(["scripts/conftest.py"], _LAYOUT_EXCEPTIONS) == []
