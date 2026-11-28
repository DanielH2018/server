#!/usr/bin/env python3
"""Files a pull request under its session milestone, and closes a milestone once it is done.

The `session-milestone` workflow runs both subcommands. `findings_lib/milestone.py` owns
the scheme.

`attach-pr <n>` runs when a same-repo PR opens, reopens or has its body edited.
`milestone.pr_branch` picks the branch, and the PR joins that branch's milestone, reopening
it if it has closed. A PR whose branch never ran `findings.py milestone start` gets none.
A PR that already has a milestone keeps it, so an operator's choice is never overwritten.

`close-if-done <milestone>` runs when a PR or issue in a milestone closes, and closes the
milestone when nothing in it is left open.

Prints one line saying what it did. Exits 0 unless a gh call fails.

Typical usage example:

    session_milestone.py attach-pr 8812 --repo DanielH2018/server
"""

from dev.findings_lib.issue_model import PR_REPO
from dev.findings_lib.milestone import (
    Gh,
    GhJson,
    attach_target,
    close_if_done,
    pr_branch,
)
from lib.cli_help import ArgumentParser
from lib.gh import gh as real_gh, gh_json as real_gh_json
from lib.json_types import as_object


def attach_pr(number: str, repo: str, gh_json: GhJson, gh: Gh) -> str:
    """Files PR ``number`` under its session milestone; returns the line to print."""
    pr = as_object(
        gh_json(
            "pr",
            "view",
            number,
            "--repo",
            repo,
            "--json",
            "headRefName,milestone,closingIssuesReferences",
        ),
        f"PR #{number}",
    )
    held = pr.get("milestone")
    if held:
        return (
            f"#{number} already in milestone `{as_object(held, 'milestone')['title']}`"
        )

    def issue_milestone(n: int) -> dict | None:
        issue = as_object(
            gh_json("issue", "view", str(n), "--repo", repo, "--json", "milestone"),
            f"issue #{n}",
        )
        milestone = issue.get("milestone")
        return milestone if isinstance(milestone, dict) else None

    branch = pr_branch(pr, issue_milestone)
    title = attach_target(branch, repo, dry_run=False, gh_json=gh_json, gh=gh)
    if title is None:
        return f"#{number}: `{branch}` has no session milestone"
    gh("pr", "edit", number, "--repo", repo, "--milestone", title)
    return f"#{number} added to milestone `{title}`"


def main(
    argv: list[str] | None = None,
    *,
    gh_json: GhJson = real_gh_json,
    gh: Gh = real_gh,
) -> int:
    """Runs `attach-pr` or `close-if-done`; see the module docstring."""
    parser = ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("action", choices=["attach-pr", "close-if-done"])
    parser.add_argument("number", help="the PR for attach-pr, the milestone otherwise")
    parser.add_argument("--repo", default=PR_REPO)
    args = parser.parse_args(argv)
    if args.action == "attach-pr":
        print(attach_pr(args.number, args.repo, gh_json, gh))
        return 0
    done = close_if_done(
        int(args.number), args.repo, dry_run=False, gh_json=gh_json, gh=gh
    )
    print(
        f"milestone {args.number} "
        + ("closed" if done else "left open: not a session's, or work is open")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
