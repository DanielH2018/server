"""Whether the red tests notice each hunk of the fix when it alone is taken out.

WHY. The red gate proves each red test fails on the unchanged code, which the test author's
own loop makes true almost by construction: it reruns its tests until they fail. That says
nothing about whether the tests pin the fix. In #4213 a red test for "reports instead of
raising" asserted only that the script exited 0, and the reviewer, not the gate, found that it
missed the behaviour. This module measures the property directly, in the style of
mutation-guided test checking: a fix hunk no red test notices is a part of the fix the red
tests do not check.

HOW. After the last green gate passes, in a clean clone of HEAD (`hardened_runs.clone_at`), each
hunk of the PR's non-test diff from the merge base is reverted on its own with `git apply -R`
and the red nodes run. The merge base, not the red commit, because a batch branch that merged
master in, as #4182's and #4183's did, would otherwise count master's hunks as the fix's. A hunk is noticed when any red node stops passing. It is noticed "by absence" when
no node fails on an assertion, which is how a hunk that adds a name other hunks or the tests
import shows up; `red_tests.red_by_absence` is the classifier. A Python hunk whose revert leaves the module's
AST unchanged once docstrings are dropped, such as a comment or a docstring, is skipped, since
no test could notice it, and so is every hunk in a file `runnable` refuses, such as a doc.

It records and never refuses, as `red_tests.red_by_absence` does, until the records show where a refusal
threshold belongs. At most `MAX_HUNKS` hunks are tried, one pytest run each.
"""

import ast
import re
from itertools import pairwise
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.review.base_check import is_test_side
from fanout_lib.review.red_tests import red_by_absence
from fanout_lib.review.hardened_runs import (
    Runner,
    bare_git,
    clone_at,
    outcomes,
    pytest_argv,
    worktree_git,
)
from fanout_lib.review.red_gate import Gate
from findings_lib.red_green import suite_covered

MAX_HUNKS = 20
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.M)


@dataclass
class HunkCheck:
    """What reverting the fix one hunk at a time showed."""

    # Hunks tried, not counting skipped ones or any past `MAX_HUNKS`.
    hunks: int = 0
    # `path:line` of each tried hunk no red node noticed, the line counted at HEAD.
    missed: list[str] = field(default_factory=list)
    # Tried hunks noticed only through a missing name, never an assertion.
    by_absence: int = 0
    # Hunks no test could notice: the revert leaves the module's code unchanged, or the file
    # is not one `runnable` accepts.
    skipped: int = 0
    untried: int = 0
    error: str = ""


def red_detection(run: Runner, worktree: Path, start: str, gate: Gate) -> HunkCheck:
    """Revert each non-test hunk of `start..HEAD` on its own and run the red nodes.

    Args:
        run: the process boundary.
        worktree: the batch worktree, cloned rather than run in.
        start: the merge base of HEAD and the target branch. The red commit would do only
            on a branch that never merged the target in.
        gate: the red gate's verdict, which names the red nodes.
    """
    check = HunkCheck()
    names = worktree_git(
        run, worktree, "diff", "--name-only", "--no-renames", start, "HEAD"
    )
    paths = [p for p in names.stdout.split() if not is_test_side(p)]
    if not paths:
        return check
    head = worktree_git(run, worktree, "rev-parse", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="hunk-check-") as tmp:
        tree = Path(tmp) / "head"
        check.error = clone_at(run, worktree, head, gate.origin, tree)
        if check.error:
            return check
        for path in paths:
            diff = bare_git(
                run, tree, "diff", "-U0", "--no-color", start, head, "--", path
            )
            header, hunks = _split(diff.stdout)
            if not runnable(path):
                check.skipped += len(hunks)
                continue
            for hunk in hunks:
                if check.hunks >= MAX_HUNKS:
                    check.untried += 1
                    continue
                _try(run, tree, head, path, header + hunk, gate.nodes, check)
    return check


def runnable(path: str) -> bool:
    """Whether a test could notice a change to `path`: Python, or a file the suite covers.

    A doc or a YAML file can change in a fix, as `.claude/rules/facts.md` did in #4182's, but
    no red test reads it, so counting its hunk as missed would only dilute the measure.
    """
    return path.endswith(".py") or suite_covered(path)


def _split(diff: str) -> tuple[str, list[str]]:
    """A one-file `-U0` diff's header, and its hunks, each with its `@@` line."""
    starts = [m.start() for m in _HUNK.finditer(diff)]
    if not starts:
        return diff, []
    bounds = [*starts, len(diff)]
    return diff[: starts[0]], [diff[a:b] for a, b in pairwise(bounds)]


def _try(
    run: Runner,
    tree: Path,
    head: str,
    path: str,
    patch: str,
    nodes: list[str],
    check: HunkCheck,
) -> None:
    """Revert one hunk, run the red nodes, record what they noticed, and put it back."""
    file = tree / path
    before = file.read_text() if file.is_file() else ""
    applied = bare_git(run, tree, "apply", "-R", "--unidiff-zero", "-", stdin=patch)
    try:
        if applied.returncode:
            check.untried += 1
            return
        after = file.read_text() if file.is_file() else ""
        if path.endswith(".py") and _code(before) == _code(after):
            check.skipped += 1
            return
        check.hunks += 1
        proc = run(pytest_argv(tree, "-vv", "-rA", "--tb=no", *nodes), None)
        seen = outcomes(proc.stdout)
        failing = [n for n in nodes if seen.get(n) != "PASSED"]
        line = _HUNK.search(patch)
        if not failing:
            check.missed.append(f"{path}:{line.group(1) if line else '?'}")
        elif not _asserted(proc.stdout, failing):
            check.by_absence += 1
    finally:
        bare_git(run, tree, "checkout", "--quiet", head, "--", path)


def _asserted(output: str, failing: list[str]) -> bool:
    """Whether any of `failing` failed on an assertion rather than a missing name.

    A node that did not run at all, because its module no longer imports, counts as absence.
    """
    absent = set(red_by_absence(output, failing))
    seen = outcomes(output)
    return any(seen.get(n) == "FAILED" and n not in absent for n in failing)


def _code(source: str) -> str:
    """The module's AST without docstrings, or the source itself when it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)
