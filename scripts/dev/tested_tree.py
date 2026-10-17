#!/usr/bin/env python3
"""Say whether a master push carries a tree that a green PR run already tested.

A squash merge usually lands the tree that the PR's own run checked: the PR run checks out
`refs/pull/N/merge`, and the squash commit has the same tree whenever master did not move under
the PR. The PR run's `prek` gate records that tree on the PR head commit as a commit status with
context `ci/tested-tree`, success state and the tree id as its description. This script is the
reader. The push run of ci.yml calls it first, and every other job skips when it answers
`proven=true`.

It fails safe in every direction: any error, any missing status and any mismatch answers
`proven=false`, and the push run then sweeps in full. It exits 0 in all of those cases, so a
broken lookup never turns master red by itself.

Writes `proven=true|false` to the file named by $GITHUB_OUTPUT when that variable is set, and
prints the answer with its reason.

Run: python3 scripts/dev/tested_tree.py prove --repo OWNER/NAME --sha SHA --tree TREE
"""

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser

# The commit-status context the PR run writes and this script reads. ci.yml's `prek` job writes
# it; ansible/tests/repo/test_ci_skip_proven_master.py pins both sides to this name.
CONTEXT = "ci/tested-tree"

_OBJECT_ID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_TIMEOUT_S = 20

Rows = list[dict[str, Any]]
Get = Callable[[str], Rows]


def tree_is_proven(get: Get, repo: str, sha: str, tree: str) -> tuple[bool, str]:
    """Whether `tree` was recorded green on the head of the PR that `sha` squash-merged.

    `get` maps an API path to its decoded JSON and raises on any failure. Returns the answer
    and the reason, which the push run prints.
    """
    if not (_OBJECT_ID.fullmatch(sha) and _OBJECT_ID.fullmatch(tree)):
        return False, "the commit or its tree id is unreadable"
    try:
        pulls = get(f"repos/{repo}/commits/{sha}/pulls")
        # Only a PR that this commit IS the merge of counts. A commit can be listed under a PR
        # that merely contains it, and that PR's run tested some other tree.
        heads = [p["head"]["sha"] for p in pulls if p.get("merge_commit_sha") == sha]
        if not heads:
            return False, "no pull request merged as this commit"
        for head in heads:
            statuses = get(f"repos/{repo}/commits/{head}/statuses?per_page=100")
            for status in statuses:
                if (
                    status.get("context") == CONTEXT
                    and status.get("state") == "success"
                    and status.get("description") == tree
                ):
                    return (
                        True,
                        f"PR head {head[:8]} recorded a green run on tree {tree[:8]}",
                    )
        return (
            False,
            f"no green {CONTEXT} status on the PR head carries tree {tree[:8]}",
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return False, f"the lookup failed: {type(exc).__name__}: {exc}"


def _api_get(token: str) -> Get:
    def get(path: str) -> Rows:
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "tested-tree",
            },
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.load(response)

    return get


def main(argv: list[str] | None = None, get: Get | None = None) -> int:
    parser = ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prove = sub.add_parser("prove", help="answer proven=true|false for one push")
    prove.add_argument("--repo", required=True, help="OWNER/NAME")
    prove.add_argument("--sha", required=True, help="the pushed commit")
    prove.add_argument("--tree", required=True, help="the pushed commit's tree id")
    args = parser.parse_args(argv)

    if get is None:
        get = _api_get(os.environ.get("GITHUB_TOKEN", ""))
    proven, reason = tree_is_proven(get, args.repo, args.sha, args.tree)
    answer = f"proven={'true' if proven else 'false'}"
    print(f"{answer}: {reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(answer + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
