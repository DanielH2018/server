"""The red and green gates a `red-green` fan-out batch passes through (#3674).

WHY. A test written after the fix, by the agent that wrote the fix, can pass without checking
the property it names: #339 shipped 16 green tests for an optimisation that fired for 0 of 25
claims (`docs/failure-classes.md` class 2, "The check observes a proxy, not the property"). A
red/green batch has a separate session write the tests from the issue text alone, before any
fix exists, and this module proves those tests fail on the unchanged code.

THE RED GATE reads the test author's commit range `base..red`. It refuses the range when:

- it changes a file that is not a test: code, `conftest.py` or the pytest config could make
  the tests fail for a reason the fix never touches;
- it adds no test node, collected at `red` but not at `base`;
- running only the new nodes does not exit 1 with every node reported `FAILED`. Exit 2 is a
  collection error, such as a module-level ImportError of a module the fix will create, and a
  node that errors, skips or xfails proves nothing about the behaviour.

Only test files change in the range, so running at `red` runs the new tests against `base`'s
code.

THE GREEN GATE runs after the implementer. The red files, every `conftest.py` and
`pyproject.toml` must be unchanged since `red`, and every red node must pass.

Every process goes through the pipeline's `Runner`, so the tests script pytest's output.
"""

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.brief import _fence

# The label an issue carries to get a red phase. Only issues whose stated behaviour lives in
# Python this repo's suite runs belong under it: `scripts/`, monitor-bridge's registry, the
# filter plugins and the tested HA Jinja macros.
RED_GREEN_LABEL = "red-green"

# What the implementer may not change once the red commit exists, besides the red files.
# The `file` of the finding a failed green gate becomes, which no real path can equal.
GREEN_FILE = "(red/green gate)"
GREEN_PROTECTED = (":(glob)**/conftest.py", "pyproject.toml")

RED_SCHEMA = {
    "type": "object",
    "properties": {
        "behaviours": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "behaviour": {"type": "string"},
                    "tests": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["behaviour", "tests"],
            },
        },
    },
    "required": ["behaviours"],
}

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]

# One line of pytest's `-rA` short summary. SKIPPED lines carry a location, not a node id, so
# a skipped node is simply absent and reads as not failed.
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+?)(?: - .*)?$", re.M)


@dataclass
class Gate:
    """What the red gate decided: the reason it refused, or the files and nodes it proved."""

    reason: str = ""
    files: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reason


def review_flags(
    batch: str, issues: Sequence, review: bool, server: bool
) -> tuple[bool, bool]:
    """The `review` and `red_green` flags `launch.launch` gets for one batch.

    A batch gets the red phase when every issue in it carries `RED_GREEN_LABEL`, the launch
    asked for `--review`, and the batch is this repo's: the gates run this repo's pytest. A
    batch that carries the label and misses another condition runs without it, and says so.

    Args:
        batch: the batch id, for the note.
        issues: the batch's `brief.Issue`s.
        review: whether the launch asked for `--review`.
        server: whether the batch works this repo.
    """
    labelled = [RED_GREEN_LABEL in i.labels for i in issues]
    if not any(labelled):
        return review, False
    if all(labelled) and review and server:
        return review, True
    print(
        f"launch: batch {batch} runs without a red phase: it needs --review, this repo, "
        f"and the `{RED_GREEN_LABEL}` label on every issue",
        file=sys.stderr,
    )
    return review, False


def is_test_path(path: str) -> bool:
    """Whether the red commit may touch `path`: a `test_*.py`, or a data file under `tests/`."""
    p = PurePosixPath(path)
    if p.suffix == ".py":
        return p.name.startswith("test_")
    return "tests" in p.parts[:-1]


def outcomes(output: str) -> dict[str, str]:
    """Each node id in a `pytest -rA` run's short summary, with its outcome."""
    return {m.group(2): m.group(1) for m in _OUTCOME.finditer(output)}


def judge_red(returncode: int, output: str, nodes: list[str]) -> str:
    """Why a run of the new nodes on the unchanged code proves nothing, or "" when it does."""
    if returncode not in (0, 1):
        return f"pytest exited {returncode}: a collection or usage error, not a failing test"
    seen = outcomes(output)
    unproven = [n for n in nodes if seen.get(n) != "FAILED"]
    if unproven or returncode != 1:
        shown = ", ".join(f"{n} ({seen.get(n, 'not run')})" for n in unproven)
        return f"these new tests did not fail on the unchanged code: {shown}"
    return ""


def judge_green(returncode: int, output: str, nodes: list[str]) -> str:
    """Why the red nodes do not all pass after the fix, or "" when they do."""
    seen = outcomes(output)
    failing = [n for n in nodes if seen.get(n) != "PASSED"]
    if returncode != 0 or failing:
        shown = ", ".join(f"{n} ({seen.get(n, 'not run')})" for n in failing)
        return f"pytest exited {returncode}; not passing: {shown or 'none named'}"
    return ""


def _pytest(worktree: Path, *args: str) -> list[str]:
    # `-n0` overrides the suite's `-n auto`: a handful of nodes gains nothing from workers.
    return [
        "uv", "run", "--directory", str(worktree), "pytest",
        "-p", "no:cacheprovider", "-n0", *args,
    ]  # fmt: skip


def _git(run: Runner, worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(worktree), *args], None)


def _collect(run: Runner, worktree: Path, files: list[str]) -> tuple[set[str], int]:
    """The node ids pytest collects from `files`, and its exit code."""
    if not files:
        return set(), 5
    proc = run(_pytest(worktree, "--collect-only", "-q", *files), None)
    return {
        ln.strip() for ln in proc.stdout.splitlines() if "::" in ln
    }, proc.returncode


def red_gate(run: Runner, worktree: Path, base: str, red: str) -> Gate:
    """Prove the test author's commits `base..red` add tests that fail on `base`'s code.

    The worktree must be at `red`. Collecting at `base` checks the modified test files out
    at `base` and back again, so nothing else may run in the worktree meanwhile.

    Returns:
        The verdict; on a pass it names the red test files and the new node ids.
    """
    if base == red:
        return Gate("the test author committed nothing")
    diff = _git(run, worktree, "diff", "--name-only", "--no-renames", base, red)
    files = [f for f in diff.stdout.splitlines() if f]
    others = [f for f in files if not is_test_path(f)]
    if others:
        return Gate(
            f"the red commits change files that are not tests: {', '.join(others)}"
        )
    test_py = [f for f in files if f.endswith(".py") and (worktree / f).is_file()]
    existed = _git(run, worktree, "ls-tree", "--name-only", base, "--", *test_py)
    at_base = [f for f in existed.stdout.splitlines() if f] if test_py else []
    before: set[str] = set()
    if at_base:
        _git(run, worktree, "checkout", base, "--", *at_base)
        try:
            before, _ = _collect(run, worktree, at_base)
        finally:
            _git(run, worktree, "checkout", red, "--", *at_base)
    collected, code = _collect(run, worktree, test_py)
    if code not in (0, 5):
        return Gate(judge_red(code, "", []), files)
    nodes = sorted(collected - before)
    if not nodes:
        return Gate("the red commits add no test node", files)
    proc = run(_pytest(worktree, "-q", "-rA", "--tb=no", *nodes), None)
    reason = judge_red(proc.returncode, proc.stdout, nodes)
    return Gate(reason, files, nodes)


def green_gate(run: Runner, worktree: Path, red: str, gate: Gate) -> str:
    """Why the implementer's HEAD fails the green gate, or "" when it passes."""
    touched = _git(
        run, worktree, "diff", "--name-only", red, "HEAD", "--",
        *gate.files, *GREEN_PROTECTED,
    ).stdout.split()  # fmt: skip
    if touched:
        return f"the fix changed what the red tests stand on: {', '.join(touched)}"
    proc = run(_pytest(worktree, "-q", "-rA", "--tb=no", *gate.nodes), None)
    return judge_green(proc.returncode, proc.stdout, gate.nodes)


def red_prompt(issues: str) -> str:
    return f"""Write the failing tests for the issues below, before anyone fixes them.

Write one pytest test per behaviour the issues state, in the `tests/` directory beside the code
it covers; `.claude/rules/python-layout.md` says where. Each test must fail on the code as it
stands, through an assertion about the stated behaviour, and pass once the behaviour exists.
A gate then runs your new tests on the unchanged code and refuses them unless every one
reports FAILED. A test that errors, skips or xfails proves nothing, and neither does a
collection error. Where the fix will add a module or a function that does not exist yet,
import it inside the test body, never at module level.

Read the *Anti-patterns* section of `.claude/skills/test-scenario-hygiene/SKILL.md` first. A
test that asserts on a stub, on source text or on a mock's calls says nothing about the
behaviour.

Change only test files: `test_*.py` files and data files under a `tests/` directory. The gate
refuses a commit that touches code, a `conftest.py` or `pyproject.toml`. Do not fix the issue.
Commit the tests with `git commit`. Do not push, open a PR or comment on the issues.

Report each stated behaviour and the node ids of the tests that check it.

{issues}
"""


def red_section(red: str, gate: Gate) -> str:
    """The brief section that hands the implementer the red commit."""
    nodes = "\n".join(gate.nodes)
    fence = _fence(nodes)
    return f"""## Red tests
A separate session wrote failing tests for these issues from the issue text alone, committed
as {red} on this branch. A gate proved they fail on the code as it stands. Your change must
make them pass. Do not change them, any `conftest.py` or `pyproject.toml`: a gate after your
session runs them again and refuses the PR if you did. Where a red test is wrong, say why in
the PR body instead. The red nodes:
{fence}
{nodes}
{fence}

"""


def green_finding(reason: str) -> dict:
    """The green gate's failure as a finding the fix round acts on."""
    return {
        "title": "The PR fails the red/green gate",
        "file": GREEN_FILE,
        "severity": "high",
        "confidence": 1.0,
        "category": "test",
        "detail": reason,
    }


@dataclass(frozen=True)
class Gates:
    """The two gates a `review.Pipeline` runs; tests pass scripted ones."""

    red: Callable[[Runner, Path, str, str], Gate] = red_gate
    green: Callable[[Runner, Path, str, Gate], str] = green_gate
