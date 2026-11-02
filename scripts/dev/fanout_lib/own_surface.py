"""Which changed paths the server autolander leaves for a person (#5496, narrowed by #5659).

`autoland.problems` calls `holds` for each path a PR changes. A path under `OWN_PATHS` holds the
PR, except the two narrowings below, which read the PR's diff from GitHub.
"""

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import copy
import functools
import json
import re
import subprocess
import tomllib
from collections.abc import Callable

from fanout_lib.target import SERVER
from lib.gh import gh

# The branch `autoland.py` lands onto, and its `gh` timeout.
BASE = "master"
GH_TIMEOUT_S = 60.0
# Paths whose change widens what this command or the lander may do, or that the landing path
# imports. A PR touching one is left for a person, the way `homelab-claude-land@`'s approval
# list leaves it. `scripts/deploy_tools/` is the whole tree because land.sh imports await_ci.py and the rest
# of it, not only land*. The deployer's `files/` decide the CI gate, `ansible/vars/` holds the
# secrets and the generated cluster file, and `.claude/` holds the repo's hooks and settings.
OWN_PATHS = (
    "scripts/deploy_tools/",
    "scripts/lib/",
    "scripts/dev/fanout",
    ".github/",
    ".claude/",
    "hosts/components/claude-code/",
    "ansible/roles/setup/gitops_deploy/",
    "ansible/vars/",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
)
# Two narrowings of OWN_PATHS (#5659), and no others: skills, hooks and workflows steer what
# agents do or run as CI, so a self-landed change to one could loosen a guardrail with no
# person in the loop. `pyproject.toml` stays held unless its only change is to
# `[tool.pytest.ini_options].testpaths` (`testpaths_only`). A role's `CLAUDE.md` is prose the
# deployer never executes, so it passes under these roles and every other file there holds.
PYPROJECT = "pyproject.toml"
DOC_ONLY_ROLES = ("ansible/roles/setup/gitops_deploy/",)
DOC_FILE = "CLAUDE.md"


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


def role_docs_only(pr: int, read: Callable[[int], list[dict]]) -> bool:
    """Whether every file of PR `pr` under a `DOC_ONLY_ROLES` path is a `CLAUDE.md` it adds or edits.

    `read` returns the REST pulls-files records. A rename hides its old path from the file
    list `problems` reads, so an entry with a `previous_filename`, or any status but added or
    modified, fails when either of its paths is under a role. A rename of a task file onto
    `CLAUDE.md` deletes the task, which is a behaviour change. A failed read also fails.
    """
    try:
        entries = read(pr)
        for entry in entries:
            paths = [
                str(entry.get("filename") or ""),
                str(entry.get("previous_filename") or ""),
            ]
            if not any(p.startswith(DOC_ONLY_ROLES) for p in paths):
                continue
            plain = entry.get("status") in ("added", "modified") and not paths[1]
            if not (plain and paths[0].rsplit("/", 1)[-1] == DOC_FILE):
                return False
    except OSError, ValueError, AttributeError, subprocess.SubprocessError:
        return False
    return True


def pulls_files(pr: int) -> list[dict]:
    """Every file record of the PR, with `status` and `previous_filename`; raises on a failed read."""
    out = gh(
        "api", "--paginate", f"repos/{SERVER}/pulls/{pr}/files",
        "--jq", ".[] | {filename, status, previous_filename}", timeout=GH_TIMEOUT_S,
    )  # fmt: skip
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


@functools.cache
def live_role_docs_only(pr: int) -> bool:
    """`role_docs_only` against GitHub, read once per PR for the passes of one run."""
    return role_docs_only(pr, pulls_files)


def holds(
    path: str, pyproject_ok: Callable[[], bool], docs_ok: Callable[[], bool]
) -> bool:
    """Whether this changed path is one the lander leaves for a person."""
    if path == PYPROJECT:
        return not pyproject_ok()
    if path.startswith(DOC_ONLY_ROLES) and path.rsplit("/", 1)[-1] == DOC_FILE:
        return not docs_ok()
    return path.startswith(OWN_PATHS)
