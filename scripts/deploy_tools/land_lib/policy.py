"""The landing policy a lander unit sets for an agent's PRs, checked before any merge call.

Two environment settings switch it on. A landing that sets neither runs exactly as before,
which is every interactive session and renovate-agent's lander:

- `LAND_REQUIRE_BRANCH_PREFIX`: the head branch must be in this repo, start with the prefix,
  and target master. The agent's GitHub account can push only under its own prefix ("agent
  branch fence", ruleset 24517167), so this keeps the lander to the branches the fence gives
  the agent.
- `LAND_APPROVAL_PATHS`: a file of path prefixes, one per line. A PR changing a path under one
  of them is refused. Those are the paths that widen the agent's own authority: its roles, its
  credentials, the rulesets' drift checks and this code.
- `LAND_APPROVER`: a GitHub login. A PR the approval list refuses lands anyway when this
  login's latest review is an approval of the head SHA the checks read. A later
  changes-requested or a dismissal undoes it, and so does a push: the approval then names an
  older commit. Unset, nothing lifts the refusal.

When either of the first two is set, the policy also refuses while the deployer holds a SHA, and `check`
returns the head SHA it checked. `merge.py` pins every merge path to that SHA, so a push after
the checks fails the merge rather than landing unchecked.

WHY IN land.sh AND NOT IN THE LANDER. The operator chose on 2026-10-05 that landing logic lives
here rather than being re-implemented in a unit's own script. The approval list therefore
names `scripts/deploy_tools/land`, so a PR that weakens these checks needs the operator's
approval as well.
"""

import subprocess
import sys as _sys
from pathlib import Path as _Path
from typing import Any, NoReturn

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say

# GitHub's REST files endpoint returns at most 3000 files, so a PR at the cap may hide a path
# the list names.
FILE_CAP = 3000
# The review states that change whether a PR is approved. A COMMENTED review after an
# approval leaves it approved, as it does for GitHub's own review rule.
VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def read_approval_paths(path: str) -> list[str]:
    """The prefixes in the approval-path file, without blank lines and `#` comments.

    Raises:
        OSError: the file cannot be read.
        ValueError: the file names no prefix. An empty list would approve every path, so it
            is refused rather than read as "nothing needs approval".
    """
    lines = _Path(path).read_text().splitlines()
    prefixes = [
        s for s in (line.strip() for line in lines) if s and not s.startswith("#")
    ]
    if not prefixes:
        raise ValueError(f"{path} names no path")
    return prefixes


def branch_problems(view: dict[str, Any], prefix: str) -> list[str]:
    """Why the PR's head branch is not one the policy lands; empty when it is."""
    problems = []
    if view.get("isCrossRepository") is not False:
        problems.append("its head branch is not in this repo")
    if view.get("baseRefName") != BRANCH:
        problems.append(
            f"it targets {view.get('baseRefName') or '<unknown>'}, not {BRANCH}"
        )
    name = view.get("headRefName") or ""
    if not name.startswith(prefix):
        problems.append(f"its branch {name or '<unknown>'} is outside {prefix}")
    return problems


def approval_hits(files: list[dict[str, Any]], prefixes: list[str]) -> list[str]:
    """Each changed path under an approval prefix, in order, without duplicates.

    A renamed file is checked under its old path too, so a rename out of a listed directory
    still needs approval.
    """
    hits: list[str] = []
    for entry in files:
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if name and name not in hits and name.startswith(tuple(prefixes)):
                hits.append(name)
    return hits


def approval_problem(reviews: list[dict[str, Any]], approver: str, head: str) -> str:
    """Why `approver` has not approved `head`; empty when their latest verdict approves it."""
    theirs = sorted(
        (
            r
            for r in reviews
            if (r.get("user") or {}).get("login") == approver
            and r.get("state") in VERDICT_STATES
        ),
        key=lambda r: r.get("submitted_at") or "",
    )
    if not theirs:
        return f"{approver} has not approved it"
    latest = theirs[-1]
    if latest["state"] != "APPROVED":
        return f"{approver}'s latest review is {latest['state'].lower()}"
    approved = latest.get("commit_id") or ""
    if approved != head:
        return f"{approver} approved {approved[:8] or '<unknown>'}, not the head {head[:8]}"
    return ""


def _refuse(ln: Landing, why: str) -> NoReturn:
    ln.die(f"refused by the landing policy: {why}", 1)


def _list(ln: Landing, what: str) -> list[dict[str, Any]]:
    """Every entry of the PR's REST `what` listing (`files`, `reviews`), across all pages."""
    try:
        pages = ln.tools.gh_json(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/{what}",
        )
    except subprocess.CalledProcessError as exc:
        _refuse(ln, f"could not list the PR's {what}: {exc.stderr.strip()}")
    except subprocess.TimeoutExpired:
        _refuse(ln, f"could not list the PR's {what}: gh timed out")
    except ValueError:
        _refuse(ln, f"could not list the PR's {what}: unparseable gh output")
    return [entry for page in pages or [] for entry in page]


def check(ln: Landing) -> str:
    """Run every policy check, or die refusing; the head SHA the checks read.

    The head is read again after the file list, so the list and the returned SHA describe the
    same commit.
    """
    o = ln.opts
    view = ln.view("headRefOid,headRefName,baseRefName,isCrossRepository")
    head = view.get("headRefOid") or ""
    if not head:
        _refuse(ln, "GitHub returned no head SHA to pin the merge to")
    if o.require_branch_prefix:
        problems = branch_problems(view, o.require_branch_prefix)
        if problems:
            _refuse(ln, "; ".join(problems))
    hold = ln.state("hold_sha")
    if hold is None:
        _refuse(ln, "the deployer's hold_sha could not be read")
    if hold:
        _refuse(
            ln,
            f"the deployer is holding {hold}; a merge now would deploy on top of a failed "
            "apply. Clear the hold first (CLAUDE.md, When to wait)",
        )
    if o.approval_paths:
        try:
            prefixes = read_approval_paths(o.approval_paths)
        except (OSError, ValueError) as exc:
            _refuse(ln, f"the approval-path list is unusable: {exc}")
        # The REST listing, because it also names a rename's old path.
        files = _list(ln, "files")
        if len(files) >= FILE_CAP:
            _refuse(ln, f"it changes {len(files)} files, at GitHub's listing cap")
        hits = approval_hits(files, prefixes)
        if hits:
            why = (
                f"it changes paths that need the operator's approval: {', '.join(hits)}"
            )
            if not o.approver:
                _refuse(ln, why)
            problem = approval_problem(_list(ln, "reviews"), o.approver, head)
            if problem:
                _refuse(ln, f"{why}; {problem}")
            say(f"{o.approver} approved {head[:8]}, lifting the approval-path refusal")
    again = ln.view("headRefOid").get("headRefOid") or ""
    if again != head:
        _refuse(
            ln, f"the head moved during the checks ({head[:8]} -> {again[:8]}); re-run"
        )
    say(f"landing policy passed at {head[:8]}")
    return head
