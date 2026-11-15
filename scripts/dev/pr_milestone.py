#!/usr/bin/env python3
"""Adds a pull request to its Claude session's milestone.

The `pr-milestone` workflow runs this when a same-repo PR opens, reopens or has its body
edited. `findings_lib/milestone.py:pr_title` picks the milestone, and the script creates or
reopens it first. A PR that already has a milestone keeps it, so an operator's choice is
never overwritten. A PR that is not a session's, such as Renovate's, is left alone.

Prints one line saying what it did. Exits 0 unless a gh call fails.

Typical usage example:

    pr_milestone.py 8812 --repo DanielH2018/server
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.findings_lib.issue_model import PR_REPO
from dev.findings_lib.milestone import Gh, GhJson, ensure, pr_title
from lib.cli_help import ArgumentParser
from lib.gh import gh as real_gh, gh_json as real_gh_json
from lib.json_types import as_object


def main(
    argv: list[str] | None = None,
    *,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> int:
    """Adds PR ``number`` to its session milestone; see the module docstring."""
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("number")
    parser.add_argument("--repo", default=PR_REPO)
    args = parser.parse_args(argv)
    pr = as_object(
        gh_json(
            "pr",
            "view",
            args.number,
            "--repo",
            args.repo,
            "--json",
            "headRefName,milestone,closingIssuesReferences",
        ),
        f"PR #{args.number}",
    )
    held = pr.get("milestone")
    if held:
        print(
            f"#{args.number} already in milestone `{as_object(held, 'milestone')['title']}`"
        )
        return 0

    def issue_milestone(n: int) -> dict | None:
        issue = as_object(
            gh_json(
                "issue", "view", str(n), "--repo", args.repo, "--json", "milestone"
            ),
            f"issue #{n}",
        )
        milestone = issue.get("milestone")
        return milestone if isinstance(milestone, dict) else None

    title = pr_title(pr, issue_milestone)
    if not title:
        print(f"#{args.number} is not a session's PR; no milestone")
        return 0
    ensure(title, args.repo, dry_run=False, gh_json=gh_json, gh=gh)
    gh("pr", "edit", args.number, "--repo", args.repo, "--milestone", title)
    print(f"#{args.number} added to milestone `{title}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
