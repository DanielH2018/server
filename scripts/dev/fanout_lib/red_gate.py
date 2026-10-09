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
  node that errors, skips or xfails proves nothing about the behaviour.

Only test files change in the range, so running at `red` runs the new tests against `base`'s
code.

THE GREEN GATE runs after the implementer and again after every fix round. The working tree
must match HEAD, because the PR ships HEAD: an uncommitted edit to a red test or to the code
would otherwise pass a gate the pushed head fails (#3821). The red files, every pytest config
file and the `leakguard` plugin must be unchanged since `red`, no untracked config file may
exist, and every red node must pass in a fresh clone of HEAD. The clone is what makes the
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

import re
import subprocess
import sys
import tempfile
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
    # `refs/remotes/origin/master` when the red gate ran, "" when there was none. Every
    # worktree shares that ref, so the green gate pins its clone's copy here (#3845).
    origin: str = ""

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
    if p.name in PYTEST_CONFIG or path in PLUGINS:
        return False
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
        "-p", "no:cacheprovider", "-n0", "-c", "pyproject.toml", *args,
    ]  # fmt: skip


# git with no configuration the implementer can write: no global or system file, which a
# clone's own fresh config leaves as the only other sources, and no user attributes file. The
# green gate checks HEAD out this way, so no filter, `info/attributes` line or config value
# from the worktree's repo can rewrite a file on its way to pytest (#3837).
_BARE_GIT = (
    "env", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
    "git", "-c", "core.attributesFile=/dev/null",
)  # fmt: skip


def _git(run: Runner, worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(worktree), *args], None)


ORIGIN_MASTER = "refs/remotes/origin/master"


def _origin_refs(run: Runner, clone: Path, origin: str) -> str:
    """The `update-ref --stdin` input that leaves `clone` only the red gate's `origin/master`.

    A clone of a path maps the source's local branches to `origin/*`, so its `origin/master`
    is the source's local `master`. Tests that diff against `origin/master` must see the base
    the red gate saw, not that branch and not the source's live remote-tracking ref, which a
    fetch or the implementer can move between the gates (#3845). One transaction may not name
    a ref twice, so `origin/master` is set rather than deleted and recreated.
    """
    mapped = run(
        [
            *_BARE_GIT, "-C", str(clone), "for-each-ref", "--format=%(refname)",
            "refs/remotes/origin/",
        ],
        None,
    ).stdout.split()  # fmt: skip
    lines = [f"delete {ref}" for ref in mapped if not (origin and ref == ORIGIN_MASTER)]
    if origin:
        lines.append(f"update {ORIGIN_MASTER} {origin}")
    return "".join(f"{line}\n" for line in lines)


def stray_config(run: Runner, worktree: Path) -> list[str]:
    """Untracked files pytest would read as configuration, ignored ones included.

    `git status` and `git diff` see neither kind, and this repo's `.gitignore` ignores every
    root path, so a root `conftest.py` is invisible to both. `--directory` folds a wholly
    untracked directory into one entry; pytest reads a conftest only from a test's own
    directories, which hold tracked files and so are never folded.
    """
    listed = _git(run, worktree, "ls-files", "--others", "--directory").stdout
    return [f for f in listed.splitlines() if PurePosixPath(f).name in PYTEST_CONFIG]


def ignored_claude_files(run: Runner, worktree: Path) -> set[str]:
    """The ignored files under `worktree`'s `.claude/`.

    Neither the red range nor `git status` shows an ignored file, and `.gitignore` ignores
    `.claude/*.local.json`, so a red author could plant a `.claude/settings.local.json` that
    widens what a later session may run (#3838). The pipeline deletes each one the red phase
    added.
    """
    listed = _git(
        run, worktree, "ls-files", "--others", "--ignored", "--exclude-standard",
        "--", ".claude/",
    ).stdout  # fmt: skip
    return set(listed.splitlines())


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
    origin = _git(
        run, worktree, "rev-parse", "--verify", "--quiet", f"{ORIGIN_MASTER}^{{commit}}"
    ).stdout.strip()
    # An uncommitted edit to the code would make the tests fail for a reason the range
    # never shows, and a rewritten base would carry other history onto the PR branch.
    dirty = _git(run, worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        return Gate(f"the test author left uncommitted changes: {dirty}")
    stray = stray_config(run, worktree)
    if stray:
        return Gate(f"untracked pytest configuration: {', '.join(stray)}")
    if _git(run, worktree, "merge-base", "--is-ancestor", base, red).returncode:
        return Gate(f"the red commits do not descend from the base {base}")
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
    return Gate(reason, files, nodes, origin)


def green_gate(run: Runner, worktree: Path, red: str, gate: Gate) -> str:
    """Why the implementer's HEAD fails the green gate, or "" when it passes."""
    dirty = _git(run, worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        return (
            "the tree pytest would run differs from the HEAD the PR ships; commit or "
            f"discard these changes: {dirty}"
        )
    touched = _git(
        run, worktree, "diff", "--name-only", red, "HEAD", "--",
        *gate.files, *GREEN_PROTECTED,
    ).stdout.split()  # fmt: skip
    if touched:
        return f"the fix changed what the red tests stand on: {', '.join(touched)}"
    stray = stray_config(run, worktree)
    if stray:
        return f"untracked pytest configuration: {', '.join(stray)}"
    head = _git(run, worktree, "rev-parse", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="green-gate-") as tmp:
        tree = Path(tmp) / "head"
        cloned = run(
            [
                *_BARE_GIT, "clone", "--quiet", "--shared", "--no-checkout",
                "--template=", str(worktree), str(tree),
            ],
            None,
        )  # fmt: skip
        if cloned.returncode == 0:
            cloned = run(
                [*_BARE_GIT, "-C", str(tree), "update-ref", "--no-deref", "--stdin"],
                _origin_refs(run, tree, gate.origin),
            )
        if cloned.returncode == 0:
            cloned = run(
                [*_BARE_GIT, "-C", str(tree), "checkout", "--quiet", "--detach", head],
                None,
            )
        if cloned.returncode:
            return f"could not check HEAD out to run the red tests: {cloned.stderr.strip()}"
        proc = run(_pytest(tree, "-q", "-rA", "--tb=no", *gate.nodes), None)
    return judge_green(proc.returncode, proc.stdout, gate.nodes)


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

Write one pytest test per behaviour the issues state, in the `tests/` directory beside the code
it covers; `.claude/rules/python-layout.md` says where. Each test must fail on the code as it
stands, through an assertion about the stated behaviour, and pass once the behaviour exists.
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


def green_finding(reason: str, red: str, gate: Gate) -> dict:
    """The green gate's failure as a finding the fix round acts on, with how to undo it."""
    restore = f"git checkout {red} -- {' '.join(gate.files)}"
    return {
        "title": "The PR fails the red/green gate",
        "file": GREEN_FILE,
        "severity": "high",
        "confidence": 1.0,
        "category": "test",
        "detail": (
            f"{reason}. The red tests are the ones committed at {red}; restore an edited "
            f"one with `{restore}` and make the code pass them instead."
        ),
    }


@dataclass(frozen=True)
class Gates:
    """The two gates a `review.Pipeline` runs; tests pass scripted ones."""

    red: Callable[[Runner, Path, str, str], Gate] = red_gate
    green: Callable[[Runner, Path, str, Gate], str] = green_gate
