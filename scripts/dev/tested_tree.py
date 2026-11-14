#!/usr/bin/env python3
"""Say whether a CI run would test a tree that a green PR run already tested.

A squash merge usually lands the tree that the PR's own run checked: the PR run checks out
`refs/pull/N/merge`, and the squash commit has the same tree whenever master did not move under
the PR. The PR run's `prek` gate records that tree on the PR head commit as a commit status with
context `ci/tested-tree`, success state and the tree id as its description. The status counts
only when the Actions token created it and its `target_url` is one of this repository's Actions
runs. This script is the reader. The push run of ci.yml calls it first, and every other job
skips when it answers `proven=true`.

A pull-request run asks a second question, `prove-head`: whether the PR's head carries such a
record at all, for any tree. The run of a landing batch records one on the head of each PR it
tested stacked on master, and that PR's own run then skips as master's does. It does not
compare trees, because the tree a pull-request run would check out is the one GitHub built at
the PR's last push, not the one the batch tested. The landing compares them before it merges,
and master's run compares them again after (`docs/ci-throughput.md`).

The run of a landing batch writes those records with `record-batch`. The batch's pull request
names its members by number, and anyone who can open a pull request could name a head and a
tree of their choosing. So the command takes numbers only: it reads each member's head from
GitHub, stacks the heads on the run's base itself (`lib/tree_stack.py`), and records a tree
on a head only when the last stacked tree is the tree the run tested. One member that is not
an open pull request from a branch of this repository means no member is recorded.

It fails safe in every direction: any error, any missing status and any mismatch answers
`proven=false`, and the run then sweeps in full. `record-batch` records nothing on any
error. Each command exits 0 in all of those cases, so a broken lookup never turns a run red
by itself.

`prove` and `prove-head` write `proven=true|false` to the file named by $GITHUB_OUTPUT when
that variable is set. Every command prints its answer with the reason.

Run: python3 scripts/dev/tested_tree.py prove --repo OWNER/NAME --sha SHA --tree TREE
     python3 scripts/dev/tested_tree.py prove-head --repo OWNER/NAME --head SHA
     python3 scripts/dev/tested_tree.py record-batch --repo OWNER/NAME --base SHA \
         --tested-tree TREE --run-url URL      # reads the batch's body from $BATCH_BODY
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser
from lib.git import git
from lib.tree_stack import Conflict, stack

# The commit-status context the PR run writes and this script reads. ci.yml's `prek` job writes
# it; tests/repo/test_ci_skip_proven_master.py pins both sides to this name.
CONTEXT = "ci/tested-tree"

CREATOR = "github-actions[bot]"

# The variable that carries a batch pull request's body to `record-batch`. A body is free
# text, so it travels as data and never as part of a command line.
BODY_ENV = "BATCH_BODY"
# The line of that body that names the batch's members, as the landing writes it.
_MEMBERS = re.compile(r"^Members:((?:[ \t]+#\d+)+)[ \t]*\r?$", re.MULTILINE)
# A landing batch holds up to four pull requests (`docs/ci-throughput.md`).
MAX_MEMBERS = 4

_OBJECT_ID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_TIMEOUT_S = 20
_FETCH_TIMEOUT_S = 120

Rows = list[dict[str, Any]]
Get = Callable[[str], Rows]
Post = Callable[[str, dict[str, str]], None]


def _is_recorded_run(status: dict[str, Any], repo: str, tree: str | None) -> bool:
    """Whether `status` is the green record this repository's CI wrote for `tree`.

    With `tree` None, a record for any tree counts.
    """
    creator = status.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    url = status.get("target_url")
    recorded = status.get("description")
    return (
        status.get("context") == CONTEXT
        and status.get("state") == "success"
        and isinstance(recorded, str)
        and _OBJECT_ID.fullmatch(recorded) is not None
        and tree in (None, recorded)
        and login == CREATOR
        and isinstance(url, str)
        and re.fullmatch(
            rf"https://github\.com/{re.escape(repo)}/actions/runs/\d+", url
        )
        is not None
    )


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
                if _is_recorded_run(status, repo, tree):
                    return (
                        True,
                        f"PR head {head[:8]} recorded a green run on tree {tree[:8]}",
                    )
        return (
            False,
            f"no green {CONTEXT} status from this repository's CI on the PR head carries tree {tree[:8]}",
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return False, f"the lookup failed: {type(exc).__name__}: {exc}"


def head_is_proven(get: Get, repo: str, head: str) -> tuple[bool, str]:
    """Whether a green run of this repository's CI recorded a tested tree on `head`.

    `get` maps an API path to its decoded JSON and raises on any failure. Returns the answer
    and the reason, which the pull-request run prints.
    """
    if not _OBJECT_ID.fullmatch(head):
        return False, "the PR head is unreadable"
    try:
        statuses = get(f"repos/{repo}/commits/{head}/statuses?per_page=100")
        for status in statuses:
            if _is_recorded_run(status, repo, None):
                tree = status["description"]
                return (
                    True,
                    f"PR head {head[:8]} carries a green run on tree {tree[:8]}",
                )
        return (
            False,
            f"no green {CONTEXT} status from this repository's CI on {head[:8]}",
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return False, f"the lookup failed: {type(exc).__name__}: {exc}"


def batch_members(body: str) -> list[int]:
    """The pull-request numbers a batch's body names, in order; empty when it names none.

    A body with two `Members:` lines, a repeated number or more than `MAX_MEMBERS` numbers
    names none: the gate records only for a batch it can read one way.
    """
    lines = _MEMBERS.findall(body)
    if len(lines) != 1:
        return []
    numbers = [int(number) for number in re.findall(r"\d+", lines[0])]
    if len(set(numbers)) != len(numbers) or len(numbers) > MAX_MEMBERS:
        return []
    return numbers


def record_batch(
    get: Get,
    post: Post,
    repo: str,
    members: list[int],
    base: str,
    tested: str,
    run_url: str,
    cwd: str | Path = ".",
) -> str:
    """Record the tree a batch's run tested for each member, on the member's head.

    `get` maps an API path to its decoded JSON, and `post` sends one JSON body to an API
    path; both raise on any failure. The repository at `cwd` must hold `base`, and its
    `origin` must serve the members' heads.

    Every member is recorded, or none when the batch cannot be read, stacked or matched to
    the tested tree. A write GitHub refuses partway leaves the earlier records, and each of
    them is true.

    Returns:
        What was recorded, or why nothing was.
    """
    if not members:
        return "nothing: the pull request's body names no batch members"
    try:
        heads = []
        for number in members:
            pull: Any = get(f"repos/{repo}/pulls/{number}")
            if pull["state"] != "open" or pull["head"]["repo"]["full_name"] != repo:
                return (
                    f"nothing: #{number} is not an open pull request from a branch of "
                    "this repository"
                )
            heads.append(pull["head"]["sha"])
        git("fetch", "--no-tags", "origin", *heads, cwd=cwd, timeout=_FETCH_TIMEOUT_S)
        stacked = stack(cwd, base, heads)
        last = stacked[-1][1]
        if last != tested:
            return (
                f"nothing: the members stack to tree {last[:8]}, and the run tested "
                f"{tested[:8]}"
            )
        for head, (_, tree) in zip(heads, stacked, strict=True):
            status = {"state": "success", "context": CONTEXT, "description": tree}
            post(f"repos/{repo}/statuses/{head}", {**status, "target_url": run_url})
    except Conflict as conflict:
        return (
            f"nothing: #{members[conflict.index]} does not merge onto the run's base "
            "and the members before it"
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        return f"recording failed: {type(exc).__name__}: {exc}"
    named = (f"#{n} {head[:8]}" for n, head in zip(members, heads, strict=True))
    return "recorded " + ", ".join(named)


def _request(
    token: str, path: str, body: dict[str, str] | None
) -> urllib.request.Request:
    return urllib.request.Request(
        f"https://api.github.com/{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tested-tree",
        },
    )


def _api_post(token: str) -> Post:
    def post(path: str, body: dict[str, str]) -> None:
        with urllib.request.urlopen(_request(token, path, body), timeout=_TIMEOUT_S):
            pass

    return post


def _api_get(token: str) -> Get:
    def get(path: str) -> Rows:
        request = _request(token, path, None)
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.load(response)

    return get


def main(
    argv: list[str] | None = None, get: Get | None = None, post: Post | None = None
) -> int:
    parser = ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prove = sub.add_parser("prove", help="answer proven=true|false for one push")
    prove.add_argument("--repo", required=True, help="OWNER/NAME")
    prove.add_argument("--sha", required=True, help="the pushed commit")
    prove.add_argument("--tree", required=True, help="the pushed commit's tree id")
    prove_head = sub.add_parser(
        "prove-head", help="answer proven=true|false for one pull-request run"
    )
    prove_head.add_argument("--repo", required=True, help="OWNER/NAME")
    prove_head.add_argument("--head", required=True, help="the PR's head commit")
    batch = sub.add_parser(
        "record-batch",
        help="record what a landing batch's run tested, on each member's head",
    )
    batch.add_argument("--repo", required=True, help="OWNER/NAME")
    batch.add_argument("--base", required=True, help="the commit the run merged onto")
    batch.add_argument("--tested-tree", required=True, help="the tree the run tested")
    batch.add_argument("--run-url", required=True, help="this run's page")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if get is None:
        get = _api_get(token)
    if args.command == "record-batch":
        members = batch_members(os.environ.get(BODY_ENV, ""))
        told = record_batch(
            get,
            post or _api_post(token),
            args.repo,
            members,
            args.base,
            args.tested_tree,
            args.run_url,
        )
        print(told)
        return 0
    if args.command == "prove":
        proven, reason = tree_is_proven(get, args.repo, args.sha, args.tree)
    else:
        proven, reason = head_is_proven(get, args.repo, args.head)
    answer = f"proven={'true' if proven else 'false'}"
    print(f"{answer}: {reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(answer + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
