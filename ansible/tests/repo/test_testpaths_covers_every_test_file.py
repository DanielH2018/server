"""Guards on where a test file lives: under a `testpaths` entry, and in a `tests/` directory.

`testpaths` is hand-enumerated — a handful of roots such as `ansible/tests`, `scripts`,
`.claude/hooks` and `evals`, plus the `ansible/roles/*/*/tests` glob. Nothing derives it, so a
test in a directory none of them reaches falls outside it silently. The tests
are written, reviewed and committed; they pass when invoked directly; and `uv run pytest` never
collects them. There is no error to read, because the suite reports the count it always
reported.

The second guard keeps tests out of the directory holding the code they cover. A role's
`files/` is what the role ships, and the deployer's test-only path rule
(`deploy_changes._is_test_only_path`) is a directory check, so tests sit in a sibling `tests/`
directory. `ansible/tests/` satisfies the rule by name. `scripts/conftest.py` is the
one deliberate exception, the shared conftest for the whole `scripts` testpath.

CI's `pytest` job runs the whole suite on every PR with no file scope, so what `testpaths`
reaches is the gap this module covers.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED:
a guard that matches everything and a guard that matches nothing are indistinguishable from the
passing side alone.
"""

import tomllib
from pathlib import PurePosixPath

import pytest_shard
from _helpers import REPO
from lib.proc_testing import run

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
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    return sorted(
        rel
        for rel in listed.split("\0")
        if rel and any(PurePosixPath(rel).match(g) for g in _TEST_FILE_GLOBS)
    )


def orphaned_test_files(test_files, testpaths) -> list[str]:
    """The test files lying under no `testpaths` entry, and so never collected.

    Matching is by path component rather than string prefix: a `scripts` entry must not be read
    as covering `scripts_extra/test_x.py`. `pytest_shard.under_testpaths` gives that, and also
    expands a glob entry such as `ansible/roles/*/*/tests`, where a `str.startswith` would
    silently pass the exact file this guard exists to catch.
    """
    return [
        path for path in test_files if not pytest_shard.under_testpaths(path, testpaths)
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


def test_a_glob_testpath_covers_role_tests_and_not_role_files() -> None:
    orphans = orphaned_test_files(
        [
            "ansible/roles/k8s/newthing/tests/test_a.py",
            "ansible/roles/k8s/newthing/files/test_b.py",
        ],
        ["ansible/roles/*/*/tests"],
    )
    assert orphans == ["ansible/roles/k8s/newthing/files/test_b.py"]


# Test-suite files that may sit outside a `tests/` directory, and why.
_LAYOUT_EXCEPTIONS = {
    # The shared conftest for the `scripts` testpath; pytest finds it by walking up from
    # each collected file, so it has to sit at the root the subdirectories share.
    "scripts/conftest.py",
    # Puts a role's own `files/` on sys.path for that role's tests (#3746). It sits above every
    # role so pytest loads it for each `<plane>/<role>/tests/`, and inside none, so no role
    # ships it.
    "ansible/roles/conftest.py",
}


def _tracked_suite_files() -> list[str]:
    """The test files plus every `conftest.py`.

    A conftest beside shipped code is the same hazard as a test beside it.
    """
    return sorted(
        set(_tracked_test_files())
        | {
            rel
            for rel in run(
                ["git", "ls-files", "-z", "--", "**/conftest.py", "conftest.py"],
                cwd=REPO,
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
