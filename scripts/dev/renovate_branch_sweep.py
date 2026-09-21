#!/usr/bin/env python3
"""Census the `renovate/*` branches no open PR and no Dependency Dashboard entry speaks for.

A `renovate/*` branch outlives the PR that carried it: the "Default" branch ruleset refuses
the app's own delete after a merge, so a branch Renovate merged survives (`renovate-prs`
skill, §8). Such a branch is invisible to every arm of `renovate-notify`, which reads open
PRs and the dashboard — an orphan appears in neither. The skill censused them through
`/tmp/rb-all` and `/tmp/rb-live` and a `comm -23`; this is that census as one command, in the
shape of `prune_worktrees.py` (#2163).

Two readings are deliberately wider than any one day needs (#1629):

  * The dashboard grep matches `<anything>-branch=renovate/...`, not the three verbs issue
    #3 happened to carry, because Renovate emits other section markers with their own
    `<verb>-branch=` prefix and an unmatched one makes every branch in that section read as
    an orphan.
  * The open-PR list carries no `--author` filter: the question is whether ANY open PR speaks
    for the branch, and §4's pattern of rebasing the bot's commit onto your own branch opens
    exactly such a PR.

Both errors run toward over-reporting; neither hides an orphan.

Report only by default. A branch with no PR is not proof the work on it is gone, and the
skill classifies each orphan (settled / dependency gone / pending update) before anything is
deleted — that reading stays in the skill. `--prune` is the operator's sweep:
`git push origin --delete <branch>` per orphan, the whole of it.

Usage:
    uv run python scripts/dev/renovate_branch_sweep.py            # list the orphans
    uv run python scripts/dev/renovate_branch_sweep.py --prune    # also delete them

Exit codes: 0 always for a report (an empty orphan set is the healthy answer); 2 `gh` or
`git` failed (its stderr is printed).
"""

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.gh import gh as _gh
from lib.git import git as _git
from lib.repo_paths import REPO

BRANCH_PREFIX = "renovate/"
# Renovate's own title for the issue it rewrites on every run; `renovate_notify` matches the
# same string (`notify_logic.DASHBOARD_TITLE`), copied rather than imported because that
# module ships into `/opt` and is not on a script's path.
DASHBOARD_TITLE = "Dependency Dashboard"
# `<verb>-branch=renovate/...` — any verb, see the module docstring.
DASHBOARD_BRANCH = re.compile(r"[a-z-]+-branch=(renovate/[^\s)]+)")

Gh = Callable[..., subprocess.CompletedProcess[str]]
Git = Callable[..., subprocess.CompletedProcess[str]]


def orphans(all_branches, open_pr_heads, dashboard_body: str) -> list[str]:
    """The `renovate/*` names in `all_branches` that neither list nor the dashboard names."""
    live = set(open_pr_heads) | set(DASHBOARD_BRANCH.findall(dashboard_body or ""))
    return sorted(
        b for b in all_branches if b.startswith(BRANCH_PREFIX) and b not in live
    )


def dashboard_body(issues: list[dict]) -> str:
    """The open Dependency Dashboard's body, or `""` when there is none (nothing is live by it)."""
    for issue in issues:
        if issue.get("title") == DASHBOARD_TITLE:
            return issue.get("body") or ""
    return ""


def _lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def census(gh: Gh = _gh) -> list[str]:
    """Ask GitHub for the three inputs and return the orphan list."""
    branches = _lines(
        gh("api", "repos/{owner}/{repo}/branches", "--paginate", "-q", ".[].name")
    )
    heads = _lines(
        gh(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "headRefName",
            "-q",
            ".[].headRefName",
        )
    )
    issues = gh(
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "title,body",
        "-q",
        f'[.[] | select(.title == "{DASHBOARD_TITLE}")]',
    ).stdout

    return orphans(branches, heads, dashboard_body(json.loads(issues or "[]")))


def main(
    argv: list[str] | None = None, gh: Gh = _gh, git: Git = _git, out=sys.stdout
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--prune", action="store_true", help="delete each orphan on origin"
    )
    args = parser.parse_args(argv)
    try:
        found = census(gh)
    except subprocess.CalledProcessError as exc:
        print(f"gh failed: {exc.stderr.strip()}", file=out)
        return 2
    if not found:
        print("no orphan renovate/* branch", file=out)
        return 0
    print(
        f"{len(found)} renovate/* branch(es) with no open PR and no dashboard entry:",
        file=out,
    )
    for branch in found:
        print(f"  {branch}", file=out)
    if not args.prune:
        print(
            "report only — classify each (renovate-prs skill, §8) before `--prune`",
            file=out,
        )
        return 0
    for branch in found:
        try:
            git("push", "origin", "--delete", branch, cwd=REPO)
        except subprocess.CalledProcessError as exc:
            print(
                f"git push origin --delete {branch} failed: {exc.stderr.strip()}",
                file=out,
            )
            return 2
        print(f"deleted origin/{branch}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
