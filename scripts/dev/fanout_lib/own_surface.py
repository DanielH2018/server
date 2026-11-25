"""Which daniel-server batch PRs an unattended landing leaves for a person.

`pr_problems` is the PR half of the rule. `autoland.problems` adds the review record's half for
the server lander, and a daniel-server review pipeline asks it before it lands its own draft
(`review_ci.server_lands`). A path under `OWN_PATHS` holds the PR, except `pyproject.toml` when
its only change is to `testpaths`, which reads the PR's diff from GitHub.
"""

import copy
import functools
import re
import subprocess
import tomllib
from collections.abc import Callable

from dev.fanout_lib.target import SERVER
from lib.gh import gh

# The branch a server batch's PR lands onto, and the `gh` timeout.
BASE = "master"
GH_TIMEOUT_S = 60.0
BRANCH_PREFIX = "worktree-fanout-"
# The login every server batch's PR carries. The server lander's LAND_REQUIRE_AUTHOR pins the same.
AUTHOR = "DanielH2018"
# gh returns at most this many changed files, so a PR at the cap may hide a path past it.
FILE_CAP = 100
# A heading that names a step only a person can do, and the text that holds a PR back.
OPERATOR_HEADING = re.compile(r"(?i)before[- ]merge|operator")
HOLD_TEXT = re.compile(r"(?i)needs input|do not merge")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)")
UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]")
# The `gh pr view --json` fields `pr_problems` reads.
PR_FIELDS = "headRefName,headRefOid,baseRefName,body,author,files,isCrossRepository"
# Paths whose change widens what the lander may do, or that the landing path imports; a PR touching
# one is left for a person. `scripts/deploy_tools/` is the whole tree because land.sh imports all of
# it.
OWN_PATHS = (
    "scripts/deploy_tools/",
    "scripts/lib/",
    "scripts/dev/fanout",
    ".github/",
    ".claude/",
    "hosts/components/claude-code/",
    "secrets/",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
)
# One narrowing of OWN_PATHS: `pyproject.toml` stays held unless its only change is
# `[tool.pytest.ini_options].testpaths`. Skills, hooks and workflows steer agents, so a self-landed
# change could loosen a guardrail.
PYPROJECT = "pyproject.toml"


def gh_raw_file(path: str, ref: str) -> str:
    """The text of `path` at commit `ref`; raises on a failed read."""
    out = gh(
        "api", "-H", "Accept: application/vnd.github.raw",
        f"repos/{SERVER}/contents/{path}?ref={ref}", timeout=GH_TIMEOUT_S,
    )  # fmt: skip
    return out.stdout


def merge_base(head: str) -> str:
    """The commit the PR branched from, which is what its diff is against."""
    out = gh(
        "api", f"repos/{SERVER}/compare/{BASE}...{head}",
        "--jq", ".merge_base_commit.sha", timeout=GH_TIMEOUT_S,
    )  # fmt: skip
    sha = out.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"no merge base for {head}")
    return sha


def without_testpaths(config: dict) -> dict:
    """`config` with `[tool.pytest.ini_options].testpaths` removed, where it exists."""
    rest = copy.deepcopy(config)
    table = rest.get("tool", {}).get("pytest", {}).get("ini_options")
    if isinstance(table, dict):
        table.pop("testpaths", None)
    return rest


def pyproject_pair(head: str) -> tuple[str, str]:
    """`pyproject.toml` at the PR's merge base and at its head; raises on a failed read."""
    return gh_raw_file(PYPROJECT, merge_base(head)), gh_raw_file(PYPROJECT, head)


def testpaths_only(head: str, read: Callable[[str], tuple[str, str]]) -> bool:
    """Whether the PR at `head` changes `pyproject.toml` only inside `testpaths`.

    `read` returns the file at the merge base and at `head`. This compares the two parsed, so
    a dependency, a `[tool.uv]` key or any other table that differs holds the PR. Any read or
    parse failure also holds it: this is the fail-closed half of the narrowing.
    """
    try:
        old, new = (tomllib.loads(text) for text in read(head))
        paths = (
            new.get("tool", {})
            .get("pytest", {})
            .get("ini_options", {})
            .get("testpaths", [])
        )
    except OSError, ValueError, subprocess.SubprocessError:
        return False
    listed = isinstance(paths, list) and all(isinstance(p, str) for p in paths)
    return listed and without_testpaths(old) == without_testpaths(new)


@functools.cache
def live_testpaths_only(head: str) -> bool:
    """`testpaths_only` against GitHub, read once per head for the passes of one run."""
    return testpaths_only(head, pyproject_pair)


def holds(path: str, pyproject_ok: Callable[[], bool]) -> bool:
    """Whether this changed path is one the lander leaves for a person."""
    if path == PYPROJECT:
        return not pyproject_ok()
    return path.startswith(OWN_PATHS)


def operator_step_in(body: str) -> str:
    """Why the PR body holds the PR back, or "" when it does not.

    An unchecked box under a heading that names an operator or before-merge step, or text
    saying the PR needs input or must not merge. The template's own unchecked deploy boxes sit
    under other headings and do not count.
    """
    heading = ""
    for line in body.splitlines():
        found = HEADING.match(line)
        if found:
            heading = found.group(1).strip()
        elif heading and OPERATOR_HEADING.search(heading) and UNCHECKED.match(line):
            return f"an unchecked box under '{heading}'"
    hold = HOLD_TEXT.search(body)
    return f"the body says '{hold.group(0)}'" if hold else ""


def never(_key: str | int) -> bool:
    return False


def pr_problems(
    pr: dict, batch: str, pyproject_ok: Callable[[str], bool] = never
) -> str:
    """Why batch `batch`'s PR may not land unattended, or "" when it may. First reason wins.

    `pr` is the PR as `gh pr view --json` reads `PR_FIELDS`. `pyproject_ok` answers
    `testpaths_only` for the PR's head; the default holds every PR that touches
    `pyproject.toml`.
    """
    if pr.get("isCrossRepository") or pr.get("baseRefName") != BASE:
        return f"the PR does not target {BASE} from this repo"
    if pr.get("headRefName") != f"{BRANCH_PREFIX}{batch}":
        return f"the head branch is '{pr.get('headRefName')}', not the batch's"
    if (pr.get("author") or {}).get("login") != AUTHOR:
        return f"the author is not {AUTHOR}"
    head = str(pr.get("headRefOid") or "")
    files = [str(f.get("path") or "") for f in pr.get("files") or []]
    if len(files) >= FILE_CAP:
        return f"it changes {len(files)} files, at the listing cap"
    own = [f for f in files if holds(f, lambda: pyproject_ok(head))]
    if own:
        return f"it changes the lander's own surface: {', '.join(own[:3])}"
    return operator_step_in(str(pr.get("body") or ""))
