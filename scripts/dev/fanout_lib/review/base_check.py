"""Which of a PR's new tests still pass with its code changes taken out.

WHY. A test that passes on the code as it stood before the fix cannot be checking what the
fix changed. The red gate proves that property for the red phase's tests, but only a batch
whose every issue carries the `red-green` label gets a red phase: 8 of the 10 review records
written on 2026-10-10 had none. In #4182 the reviewer found an implementer test that "passes on
the unchanged code" (low, 0.9) by reading it. This module finds such a test by running it.

HOW. In a clean clone of HEAD (`hardened_runs.clone_at`), every file the PR changed outside its
tests goes back to the merge base, and a file the PR added is deleted. The tests stay at HEAD.
The modules holding the PR's new test nodes then run, and the new nodes that pass are
reported. A node whose module no longer imports errors rather than passes, and
`--continue-on-collection-errors` keeps that from stopping the rest.

It reports and never refuses. A regression guard for behaviour the fix keeps passes on the
base legitimately, so the reviewer gets the list as data and judges each test, as it does for
`red_tests.red_by_absence`'s absence nodes.
"""

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.review.hardened_runs import (
    ORIGIN_MASTER,
    Runner,
    bare_git,
    clone_at,
    collect,
    outcomes,
    pytest_argv,
    worktree_git,
)


@dataclass
class BaseCheck:
    """What the check found: the PR's new test nodes, and those that pass without the fix."""

    new: int = 0
    passing: list[str] = field(default_factory=list)
    # Why the check could not run, "" when it ran.
    error: str = ""


def is_test_side(path: str) -> bool:
    """Whether `path` stays at HEAD: a test module, a conftest, or anything under `tests/`."""
    p = PurePosixPath(path)
    return (
        p.name.startswith("test_") or p.name == "conftest.py" or "tests" in p.parts[:-1]
    )


def unproven_tests(
    run: Runner, worktree: Path, start: str, head: str, exclude: Sequence[str] = ()
) -> BaseCheck:
    """Run the test nodes `start..head` adds against `start`'s code.

    Args:
        run: the process boundary.
        worktree: the batch worktree, cloned rather than run in.
        start: the merge base the PR's diff is taken from.
        head: the PR's head.
        exclude: node ids to leave out, the red phase's own.
    """
    diff = worktree_git(
        run, worktree, "diff", "--name-only", "--no-renames", start, head
    )
    changed = diff.stdout.split()
    modules = [f for f in changed if PurePosixPath(f).name.startswith("test_")]
    if not any(f.endswith(".py") for f in modules):
        return BaseCheck()
    origin = worktree_git(
        run, worktree, "rev-parse", "--verify", "--quiet", f"{ORIGIN_MASTER}^{{commit}}"
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="base-check-") as tmp:
        tree = Path(tmp) / "head"
        error = clone_at(run, worktree, head, origin, tree)
        if error:
            return BaseCheck(error=error)
        present = [f for f in modules if f.endswith(".py") and (tree / f).is_file()]
        before = _nodes_at(run, tree, start, head, present)
        after, _ = collect(run, tree, present)
        new = sorted(after - before - set(exclude))
        if not new:
            return BaseCheck()
        code = [f for f in changed if not is_test_side(f)]
        kept = _existing(run, tree, start, code)
        if kept:
            bare_git(run, tree, "checkout", start, "--", *kept)
        for path in set(code) - set(kept):
            (tree / path).unlink(missing_ok=True)
        # Modules, not node ids: pytest aborts the whole run on a node id whose module no
        # longer imports, `--continue-on-collection-errors` notwithstanding.
        files = sorted({node.split("::")[0] for node in new})
        proc = run(
            pytest_argv(
                tree, "-vv", "-rA", "--tb=no", "--continue-on-collection-errors", *files
            ),
            None,
        )
    seen = outcomes(proc.stdout)
    return BaseCheck(len(new), [n for n in new if seen.get(n) == "PASSED"])


def _existing(run: Runner, tree: Path, rev: str, paths: list[str]) -> list[str]:
    """The `paths` that exist at `rev`."""
    if not paths:
        return []
    listed = bare_git(run, tree, "ls-tree", "-r", "--name-only", rev, "--", *paths)
    return [f for f in listed.stdout.splitlines() if f]


def _nodes_at(
    run: Runner, tree: Path, start: str, head: str, modules: list[str]
) -> set[str]:
    """The node ids `modules` held at `start`, collected against HEAD's code."""
    old = _existing(run, tree, start, modules)
    if not old:
        return set()
    bare_git(run, tree, "checkout", start, "--", *old)
    try:
        nodes, _ = collect(run, tree, old)
    finally:
        bare_git(run, tree, "checkout", head, "--", *old)
    return nodes
