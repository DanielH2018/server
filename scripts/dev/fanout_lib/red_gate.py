"""The red and green gates a `red-green` fan-out batch passes through (#3674).

WHY. A test written after the fix, by the agent that wrote the fix, can pass without checking
the property it names: #339 shipped 16 green tests for an optimisation that fired for 0 of 25
claims (`docs/failure-classes.md` class 2, "The check observes a proxy, not the property"). A
red/green batch has a separate session write the tests from the issue text alone, before any
fix exists, and this module proves those tests fail on the unchanged code.

THE RED GATE reads the test author's commit range `base..red`. It refuses the range when:

- it changes a file that is not a test: code, a `conftest.py`, a pytest config file at any
  depth or the `leakguard` plugin could make the tests fail for a reason the fix never
  touches. An untracked or ignored config file, which no diff shows, is refused too;
- it adds no test node, collected at `red` but not at `base`;
- running only the new nodes does not exit 1 with every node reported `FAILED`. Exit 2 is a
  collection error, such as a module-level ImportError of a module the fix will create, and a
  node that errors, skips or xfails proves nothing about the behaviour. The pipeline runs
  them `RUNS` times, and a node that fails in only some runs is refused as flaky.

Only test files change in the range, so running at `red` runs the new tests against `base`'s
code. In the pipeline, `reset_worktree` runs before the red gate, so the tree already holds
`red` alone and the two refusals about uncommitted and untracked files guard direct callers.

Every git call on the worktree runs under `hardened_runs.hardened`, so the repo's hooks, filters, replace
refs and the settings that move or skip a reset do not apply (#3871).

THE GREEN GATE runs after the implementer and again after every fix round. The working tree
must match HEAD, because the PR ships HEAD: an uncommitted edit to a red test or to the code
would otherwise pass a gate the pushed head fails (#3821). Every pytest config file, the
`leakguard` plugin and any red data file must be unchanged since `red`. A red `test_*.py` may
only gain appended tests, which `red_tests.append_only` defines (#4214). No untracked config file may
exist, and every red node must pass in a fresh clone of HEAD, in every one of the pipeline's
`RUNS` runs. The clone is what makes the
verdict HEAD's: the implementer controls the worktree's index and git config, so a
skip-worktree entry, an `info/exclude` line or `status.showUntrackedFiles=no` each hides an
edit from `git status`. The clone reads none of that repo's config or `info/attributes`, so a
smudge filter cannot rewrite a test on checkout either, and it is a repository, so a red test
that runs `git ls-files` passes there as it would in CI (#3837). Its `origin/master` is the
SHA the red gate read, so a test that diffs against it sees the same base at both gates
(#3845). It shares the worktree's
object store, which the agent can also write; CI's run is the backstop there too.

Both gates run pytest with `-c pyproject.toml`, so the root configuration decides every run.
Every process here runs as the agent's own user, so a determined agent can still edit the
virtualenv pytest runs from; CI's run on the pushed head is the backstop for that.

Every process goes through the pipeline's `Runner`, so the tests script pytest's output.
"""

import json
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.brief import fence_for
from fanout_lib.red_tests import append_only, red_by_absence

from fanout_lib.hardened_runs import (
    ORIGIN_MASTER,
    Runner,
    clone_at,
    collect,
    outcomes,
    pytest_argv,
    worktree_git,
)


# The label an issue carries to get a red phase. `findings.py open` applies it to a finding
# whose every cited path is code the suite runs (`findings_lib.red_green`).
from findings_lib.red_green import RED_GREEN_LABEL

# What the implementer may not change once the red commit exists, besides the red files.
# The `file` of the finding a failed green gate becomes, which no real path can equal.
GREEN_FILE = "(red/green gate)"
# How often the pipeline's gates run the red nodes; `Gates` says why more than once.
RUNS = 3
# Files pytest reads as configuration wherever they sit: a nested inifile replaces the root
# one for nodes below it, and a conftest can rewrite any outcome.
PYTEST_CONFIG = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
    }
)
# `pyproject.toml`'s addopts loads this plugin into every run with `-p leakguard`.
PLUGINS = ("ansible/tests/leakguard.py",)
GREEN_PROTECTED = (*(f":(glob)**/{name}" for name in sorted(PYTEST_CONFIG)), *PLUGINS)

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


# The suffix `green_gate` puts on a refusal when it ran the red nodes more than once.
_LATER_RUN = re.compile(r" in run (\d+) of \d+$")


@dataclass
class Gate:
    """What the red gate decided: the reason it refused, or the files and nodes it proved."""

    reason: str = ""
    files: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    # `refs/remotes/origin/master` when the red gate ran, "" when there was none. Every
    # worktree shares that ref, so the green gate pins its clone's copy here (#3845).
    origin: str = ""
    # The nodes that failed on a missing name rather than an assertion (`red_tests.red_by_absence`, #4023).
    absent: list[str] = field(default_factory=list)

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
    labels = [i.labels for i in issues]
    if not any(RED_GREEN_LABEL in names for names in labels):
        return review, False
    if review and not red_skip_reason(labels, server):
        return review, True
    print(
        f"launch: batch {batch} runs without a red phase: it needs --review, this repo, "
        f"and the `{RED_GREEN_LABEL}` label on every issue",
        file=sys.stderr,
    )
    return review, False


def red_skip_reason(labels: Sequence[Sequence[str]], server: bool) -> str:
    """Why a batch whose issues carry `labels` runs no red phase; "" when it runs one (#3950)."""
    if not server:
        return "other-repo"
    labelled = [RED_GREEN_LABEL in names for names in labels]
    if not any(labelled):
        return "no-label"
    return "" if all(labelled) else "unlabelled-issue"


def labelled_skip_reason(run: Runner, batch: str, server: bool) -> str:
    """Why a review batch runs no red phase, from its issues' labels as they stand now.

    The batch id is its issue numbers joined by `-`. Every issue labelled, on a batch launched
    without the phase, means the label came after launch.
    """
    if not server:
        return "other-repo"
    labels = []
    for number in batch.split("-"):
        view = ["gh", "issue", "view", number, "--json", "labels"]
        try:
            names = json.loads(run(view, None).stdout)["labels"]
            labels.append([x["name"] for x in names])
        except ValueError, KeyError, TypeError:
            return "labels unreadable"
    return red_skip_reason(labels, True) or "labelled after launch"


def is_test_path(path: str) -> bool:
    """Whether the red commit may touch `path`: a `test_*.py`, or a data file under `tests/`."""
    p = PurePosixPath(path)
    if p.name in PYTEST_CONFIG or path in PLUGINS:
        return False
    if p.suffix == ".py":
        return p.name.startswith("test_")
    return "tests" in p.parts[:-1]


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


def green_cause(reason: str) -> str:
    """`passed`, `unmet`, `flaky` or `lock`: how one green gate run ended.

    An `unmet` first run is the red phase's real catch: the implementer's first attempt did
    not do what the red tests ask. `flaky` is a red node that passed and then failed a later
    run, which is no catch. A `lock` refusal is about what the fix touched.
    """
    if not reason:
        return "passed"
    if not reason.startswith("pytest exited"):
        return "lock"
    later = _LATER_RUN.search(reason)
    return "flaky" if later and int(later.group(1)) > 1 else "unmet"


def judge_green(returncode: int, output: str, nodes: list[str]) -> str:
    """Why the red nodes do not all pass after the fix, or "" when they do."""
    seen = outcomes(output)
    failing = [n for n in nodes if seen.get(n) != "PASSED"]
    if returncode != 0 or failing:
        shown = ", ".join(f"{n} ({seen.get(n, 'not run')})" for n in failing)
        return f"pytest exited {returncode}; not passing: {shown or 'none named'}"
    return ""


def _red_file_change(
    run: Runner, worktree: Path, red: str, path: str, gate: Gate
) -> str:
    """How the fix changed `path` beyond what `red_tests.append_only` allows, or "" when it only appended.

    A red `test_*.py` may gain tests (#4214). A red data file, a pytest config file and the
    `leakguard` plugin may not change at all, and neither may a red file be deleted.
    """
    if path not in gate.files or not path.endswith(".py"):
        return path
    blobs = [
        worktree_git(run, worktree, "cat-file", "blob", f"{rev}:{path}")
        for rev in (red, "HEAD")
    ]
    if any(b.returncode for b in blobs):
        return f"{path} deleted"
    problem = append_only(blobs[0].stdout, blobs[1].stdout)
    return f"{path} {problem}" if problem else ""


def stray_config(run: Runner, worktree: Path) -> list[str]:
    """Untracked files pytest would read as configuration, ignored ones included.

    `git status` and `git diff` see neither kind, and this repo's `.gitignore` ignores every
    root path, so a root `conftest.py` is invisible to both. `--directory` folds a wholly
    untracked directory into one entry; pytest reads a conftest only from a test's own
    directories, which hold tracked files and so are never folded.
    """
    listed = worktree_git(run, worktree, "ls-files", "--others", "--directory").stdout
    return [f for f in listed.splitlines() if PurePosixPath(f).name in PYTEST_CONFIG]


def red_gate(run: Runner, worktree: Path, base: str, red: str, runs: int = 1) -> Gate:
    """Prove the test author's commits `base..red` add tests that fail on `base`'s code.

    The worktree must be at `red`. Collecting at `base` checks the modified test files out
    at `base` and back again, so nothing else may run in the worktree meanwhile. Every one of
    `runs` runs must fail every new node, so a test that fails only sometimes is refused.

    Returns:
        The verdict; on a pass it names the red test files and the new node ids.
    """
    if base == red:
        return Gate("the test author committed nothing")
    origin = worktree_git(
        run, worktree, "rev-parse", "--verify", "--quiet", f"{ORIGIN_MASTER}^{{commit}}"
    ).stdout.strip()
    # An uncommitted edit to the code would make the tests fail for a reason the range
    # never shows, and a rewritten base would carry other history onto the PR branch.
    dirty = worktree_git(run, worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        return Gate(f"the test author left uncommitted changes: {dirty}")
    stray = stray_config(run, worktree)
    if stray:
        return Gate(f"untracked pytest configuration: {', '.join(stray)}")
    if worktree_git(run, worktree, "merge-base", "--is-ancestor", base, red).returncode:
        return Gate(f"the red commits do not descend from the base {base}")
    diff = worktree_git(run, worktree, "diff", "--name-only", "--no-renames", base, red)
    files = [f for f in diff.stdout.splitlines() if f]
    others = [f for f in files if not is_test_path(f)]
    if others:
        return Gate(
            f"the red commits change files that are not tests: {', '.join(others)}"
        )
    test_py = [f for f in files if f.endswith(".py") and (worktree / f).is_file()]
    existed = worktree_git(
        run, worktree, "ls-tree", "--name-only", base, "--", *test_py
    )
    at_base = [f for f in existed.stdout.splitlines() if f] if test_py else []
    before: set[str] = set()
    if at_base:
        worktree_git(run, worktree, "checkout", base, "--", *at_base)
        try:
            before, _ = collect(run, worktree, at_base)
        finally:
            worktree_git(run, worktree, "checkout", red, "--", *at_base)
    collected, code = collect(run, worktree, test_py)
    if code not in (0, 5):
        return Gate(judge_red(code, "", []), files)
    nodes = sorted(collected - before)
    if not nodes:
        return Gate("the red commits add no test node", files)
    # `-vv`, not `-q`: only at that verbosity is the summary's failure text left whole.
    procs = [
        run(pytest_argv(worktree, "-vv", "-rA", "--tb=no", *nodes), None)
        for _ in range(runs)
    ]
    reasons = [judge_red(p.returncode, p.stdout, nodes) for p in procs]
    failed = reasons.count("")
    reason = next((r for r in reasons if r), "")
    if reason and failed:
        reason = (
            f"failed in only {failed} of {runs} runs on the unchanged code, so it is "
            f"flaky: {reason}"
        )
    out = procs[0].stdout
    return Gate(reason, files, nodes, origin, red_by_absence(out, nodes))


def green_gate(run: Runner, worktree: Path, red: str, gate: Gate, runs: int = 1) -> str:
    """Why the implementer's HEAD fails the green gate, or "" when it passes.

    Every one of `runs` runs of the red nodes must pass, so a red test that passes only
    sometimes is not taken for a fix.
    """
    dirty = worktree_git(run, worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        return (
            "the tree pytest would run differs from the HEAD the PR ships; commit or "
            f"discard these changes: {dirty}"
        )
    touched = worktree_git(
        run, worktree, "diff", "--name-only", "--no-renames", red, "HEAD", "--",
        *gate.files, *GREEN_PROTECTED,
    ).stdout.split()  # fmt: skip
    changed = [_red_file_change(run, worktree, red, f, gate) for f in touched]
    changed = [c for c in changed if c]
    if changed:
        return f"the fix changed what the red tests stand on: {', '.join(changed)}"
    stray = stray_config(run, worktree)
    if stray:
        return f"untracked pytest configuration: {', '.join(stray)}"
    head = worktree_git(run, worktree, "rev-parse", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="green-gate-") as tmp:
        tree = Path(tmp) / "head"
        error = clone_at(run, worktree, head, gate.origin, tree)
        if error:
            return f"could not check HEAD out to run the red tests: {error}"
        for attempt in range(1, runs + 1):
            proc = run(pytest_argv(tree, "-q", "-rA", "--tb=no", *gate.nodes), None)
            reason = judge_green(proc.returncode, proc.stdout, gate.nodes)
            if reason:
                return f"{reason} in run {attempt} of {runs}" if runs > 1 else reason
    return ""


# The `test-scenario-hygiene` skill is user-level, not in this repo, so the red brief carries
# its Anti-patterns section rather than a path the test author may not have.
HYGIENE_SKILL = (
    Path.home() / ".claude" / "skills" / "test-scenario-hygiene" / "SKILL.md"
)


def anti_patterns(skill: Path = HYGIENE_SKILL) -> str:
    """The skill's `## Anti-patterns` section, or "" when the skill is not installed."""
    try:
        text = skill.read_text()
    except OSError:
        return ""
    _, sep, rest = text.partition("## Anti-patterns\n")
    return (sep + rest.split("\n## ", 1)[0]).strip() if sep else ""


def red_prompt(issues: str, anti: str = "") -> str:
    return f"""Write the failing tests for the issues below, before anyone fixes them.

Each issue's `## Verify-by` section is your primary input: write one pytest test per claim it
makes, then one per behaviour the issue states that those tests do not already check. Put each
in the `tests/` directory beside the code it covers; `.claude/rules/python-layout.md` says
where. Each test must fail on the code as it stands, through an assertion about the stated
behaviour, and pass once the behaviour exists. A claim about the tree rather than about a
function, such as "this constant is defined only in that module", is a census test: read the
tracked files with `git ls-files` and assert on what they contain.
A gate then runs your new tests on the unchanged code and refuses them unless every one
reports FAILED. A test that errors, skips or xfails proves nothing, and neither does a
collection error. Where the fix will add a module or a function that does not exist yet,
import it inside the test body, never at module level.

A test that asserts on a stub, on source text or on a mock's calls says nothing about the
behaviour. {anti or "Load the `test-scenario-hygiene` skill and read its Anti-patterns first."}

Change only test files: `test_*.py` files and data files under a `tests/` directory. The gate
refuses a commit that touches code, a `conftest.py` or `pyproject.toml`. Do not fix the issue.
Commit the tests with `git commit` and leave nothing uncommitted: the gate refuses a dirty
tree. Do not push, open a PR or comment on the issues.

Report each stated behaviour and the node ids of the tests that check it.

{issues}
"""


def red_section(red: str, gate: Gate) -> str:
    """The brief section that hands the implementer the red commit."""
    nodes = "\n".join(gate.nodes)
    fence = fence_for(nodes)
    return f"""## Red tests
A separate session wrote failing tests for these issues from the issue text alone, committed
as {red} on this branch. A gate proved they fail on the code as it stands. Your change must
make them pass. Do not change them, anything else already in their files, any `conftest.py`
or `pyproject.toml`: a gate after your session runs them again and refuses the PR if you did.
You may append new test functions, fixtures, helpers and imports to a red test file. Where a
red test is wrong, say why in the PR body instead. The red nodes:
{fence}
{nodes}
{fence}

"""


def green_finding(reason: str, red: str, gate: Gate) -> dict:
    """The green gate's failure as a finding the fix round acts on, with how to undo it.

    `git checkout <red> -- <file>` would also drop tests appended since, so the detail names
    the red version to restore from instead.
    """
    files = " ".join(gate.files)
    return {
        "title": "The PR fails the red/green gate",
        "file": GREEN_FILE,
        "severity": "high",
        "confidence": 1.0,
        "category": "test",
        "detail": (
            f"{reason}. The red tests are the ones committed at {red} in {files}. Restore "
            f"what the reason names as `git show {red}:<file>` has it, keeping any test you "
            "appended, and make the code pass the red tests instead."
        ),
    }


REPEATED_RED = partial(red_gate, runs=RUNS)
REPEATED_GREEN = partial(green_gate, runs=RUNS)


@dataclass(frozen=True)
class Gates:
    """The two gates a `review.Pipeline` runs; tests pass scripted ones.

    The pipeline's gates run the red nodes `RUNS` times. One run is not enough: #4178, the
    issue of one of the first four red batches, was itself a minute-boundary flake.
    TestGen-LLM (arXiv 2402.09171) reruns each generated test five times for the same reason.
    """

    red: Callable[[Runner, Path, str, str], Gate] = REPEATED_RED
    green: Callable[[Runner, Path, str, Gate], str] = REPEATED_GREEN
