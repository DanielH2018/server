#!/usr/bin/env python3
"""Land one Renovate PR for the unattended agent, on checks the agent cannot influence.

The agent's session runs as its own Unix user on a repo-scoped GitHub token that cannot write
repository contents, so it cannot merge. It asks for a landing by starting
`renovate-agent-land@<n>.service`, which polkit lets that user start and which runs this as the
operator's user. Every input here is read from GitHub with the operator's token or from the
operator's primary checkout. Nothing comes from a file the session can write.

A PR is landed only when all of these hold:

- GitHub names `app/renovate` as its author, which no edit to the PR can change.
- Its head branch is in this repository and named `renovate/...`, and its base is `master`.
  A token with pull-request write can retarget a base, so the base is checked too.
- It is open.
- Its branch carries no `k8s_autodeploy-false` slug, and no changed path sits under a k8s role
  the denylist names. The title is never read, because the session can edit it.
- It changes nothing under `inventory/` (`ansible/inventory/` before ADR 0030's rename). A pin there, such as `crowdsec_k8s_image` in
  `group_vars/all.yml`, reaches every role that reads it, denylisted ones included, and no path
  check can see which those are. An interactive session lands that class.
- Its `renovate/stability-days` status, when it carries one, reads success and was posted by
  `renovate[bot]`. GitHub's auto-merge waits only on the required checks, so without this a PR
  forced open mid-soak would land, and DanielClaudeBot's write access can post a status.

Then `land.sh` runs as an interactive session would run it, pinned to the head SHA these checks
read, and only its `VERDICT:` line is written where the session can read it. land.sh also needs
the operator's approval of that head when the PR changes a path on the T2 approval floor
other than by a digest-only image bump, or when anyone but `renovate[bot]` pushed to its branch
(dotfiles#967 slice 1). Stdlib only, like the rest of the agent.

Usage: land_renovate_pr.py <pr-number>

Started as `renovate-agent-land@<pr-number>.service`, never by hand. Reads the PR with
`gh`, the denylist from `scripts/dev/k8s_autodeploy_counts.py --denylist` in the checkout, and
the environment RENOVATE_AGENT_REPO (default DanielH2018/server) and RENOVATE_AGENT_REPO_DIR
(default /home/ubuntu/server). Writes the outcome to
/var/lib/renovate-agent-land/<pr-number>.verdict.
Exit codes: land.sh's own on a landing, 64 for an argument that is not a PR number, 3 for a
refusal.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import TOOLS, AgentTools, log
from host_lib import atomic_write
from renovate_floor import (
    FLOOR_FILE,
    floor_hits,
    pusher_problem,
    read_floor,
    soak_problem,
)

AUTHOR = "app/renovate"
# The login whose approval of the head lets land.sh land a PR the floor check flags.
APPROVER = "DanielH2018"
BASE = "master"
BRANCH_PREFIX = "renovate/"
DENIED_SLUG = "k8s_autodeploy-false"
# `--frozen`: a rewritten uv.lock would dirty the primary checkout, which parks the deployer.
DENYLIST_ARGV = (
    "uv",
    "run",
    "--frozen",
    "python",
    "scripts/dev/k8s_autodeploy_counts.py",
    "--denylist",
)
RESULTS_DIR = "/var/lib/renovate-agent-land"
# gh returns at most 100 changed files, so a PR at the cap may hide a denied path past it.
FILE_CAP = 100
# land.sh waits on the PR's CI, the merge, master CI, the promotion and the health gate.
# The unit's TimeoutStartSec sits above this plus its flock wait, so the script reports a
# timeout before systemd kills it mid-deploy.
# LAND_DEADLINE's 41460 plus the 22200 LAND_WINDOW_GRACE the reboot window may add (#5457).
LAND_TIMEOUT_S = 63660
PR_FIELDS = "author,isCrossRepository,baseRefName,state,headRefName,headRefOid,files"
# Both sides of ADR 0030's rename: this script is installed, so it outlives the tree it shipped in.
INVENTORY = ("inventory/", "ansible/inventory/")
# `lib/exit_codes.py:USAGE_ERROR`, which this host-shipped script cannot import (#4487).
USAGE_ERROR = 64
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,6}")
_SHA = re.compile(r"[0-9a-f]{40}")
_K8S_ROLE = re.compile(r"^(?:k8s/roles|ansible/roles/k8s)/([^/]+)/")
_VERDICT = re.compile(r"^VERDICT: .*$", re.MULTILINE)
_ROLE_LIST = re.compile(r"[a-z0-9_-]+(?:,[a-z0-9_-]+)*")


def pr_number(arg: str) -> int | None:
    """`arg` as a PR number, or None for anything but a plain positive integer."""
    return int(arg) if _PR_NUMBER.fullmatch(arg) else None


def denied_roles(output: str) -> frozenset[str] | None:
    """The k8s roles `DENYLIST_ARGV` printed, or None when its output holds no list.

    An empty list does not mean "nothing is denied", so a caller refuses rather than landing
    past it. `AgentTools.run` joins stderr after stdout, and `uv run` writes there, so the
    list is the first line and nothing else counts: a line of uv's read as role names would
    drop a denied role.
    """
    first = output.split("\n", 1)[0]
    return frozenset(first.split(",")) if _ROLE_LIST.fullmatch(first) else None


def refusals(pr: dict, denied: frozenset[str]) -> list[str]:
    """Every reason `pr` may not be landed unattended. An empty list means land it."""
    reasons = []
    if (pr.get("author") or {}).get("login") != AUTHOR:
        reasons.append(f"its author is not {AUTHOR}")
    if pr.get("isCrossRepository") is not False:
        reasons.append("its head branch is not in this repository")
    if pr.get("baseRefName") != BASE:
        reasons.append(f"its base is not {BASE}")
    if pr.get("state") != "OPEN":
        reasons.append("it is not open")
    branch = pr.get("headRefName") or ""
    if not branch.startswith(BRANCH_PREFIX):
        reasons.append(f"its head branch is not {BRANCH_PREFIX}...")
    if DENIED_SLUG in branch:
        reasons.append(f"its head branch carries {DENIED_SLUG}")
    if not _SHA.fullmatch(pr.get("headRefOid") or ""):
        reasons.append("GitHub returned no head SHA to pin the landing to")
    paths = sorted({f.get("path", "") for f in pr.get("files") or []})
    if any(path.startswith(INVENTORY) for path in paths):
        reasons.append("it changes the inventory, which can reach a denylisted role")
    if not paths or len(paths) >= FILE_CAP:
        reasons.append(f"its changed-file list is empty or at gh's {FILE_CAP}-file cap")
    for path in paths:
        match = _K8S_ROLE.match(path)
        if match and match.group(1) in denied:
            reasons.append(
                f"{path} is in {match.group(1)}, which declares k8s_autodeploy: false"
            )
    return reasons


def read_api(tools: AgentTools, path: str) -> list[dict] | None:
    """Every entry of the REST listing at `path`, across all pages, or None on a failed read."""
    rc, out = tools.run(["gh", "api", "--paginate", "--slurp", path])
    try:
        pages = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        return None
    entries = [entry for page in pages for entry in page]
    return entries if all(isinstance(e, dict) for e in entries) else None


def approval_reasons(
    files: list[dict], events: list[dict], head: str, floor: tuple[str, ...]
) -> list[str]:
    """Why the PR needs the operator's approval of `head`; empty when it lands without."""
    reasons = []
    hits = floor_hits(files, floor)
    if hits:
        reasons.append(f"it changes floor paths: {', '.join(hits)}")
    pushers = pusher_problem(events, head)
    if pushers:
        reasons.append(pushers)
    return reasons


def land_argv(land: str, number: int, head: str, approve: bool) -> list[str]:
    """The land.sh command, pinned to `head` and needing `APPROVER`'s approval when `approve`."""
    env = ["env", f"LAND_REQUIRE_HEAD={head}"]
    if approve:
        env += ["LAND_REQUIRE_APPROVAL=1", f"LAND_APPROVER={APPROVER}"]
    return [*env, land, "--pr", str(number), "--arm-merge", "--await-merge"]


def verdict_line(output: str) -> str:
    """The last `VERDICT:` line land.sh printed, or a stand-in when it printed none."""
    found = _VERDICT.findall(output)
    return found[-1] if found else "VERDICT: none (land.sh printed no verdict line)"


def main(
    argv: list[str],
    tools: AgentTools = TOOLS,
    results_dir: str = RESULTS_DIR,
    floor_file: str = FLOOR_FILE,
) -> int:
    """Check PR `argv[1]`, land it if every check passes, and record the outcome.

    Returns land.sh's exit code, 64 for a malformed argument and 3 for a refusal. The result
    file is overwritten before anything else runs, so the session never reads an earlier
    landing's verdict as this one's.
    """
    number = pr_number(argv[1]) if len(argv) == 2 else None
    if number is None:
        print(
            f"land_renovate_pr.py: error: {argv[1:]!r} is not a PR number",
            file=sys.stderr,
        )
        return USAGE_ERROR
    result = os.path.join(results_dir, f"{number}.verdict")
    atomic_write(result, "PENDING\n")
    repo = os.environ.get("RENOVATE_AGENT_REPO", "DanielH2018/server")
    # The unit always sets RENOVATE_AGENT_REPO_DIR; the default is the operator's checkout,
    # because this file ships to the host and has no repo to derive one from.
    repo_dir = os.environ.get("RENOVATE_AGENT_REPO_DIR", "/home/ubuntu/server")

    def refuse(reason: str) -> int:
        log(f"PR #{number} refused: {reason}")
        atomic_write(result, f"REFUSED: {reason}\n")
        return 3

    rc, out = tools.run(list(DENYLIST_ARGV), cwd=repo_dir)
    if rc != 0:
        return refuse(f"k8s_autodeploy_counts.py --denylist exited {rc}")
    denied = denied_roles(out)
    if denied is None:
        return refuse("k8s_autodeploy_counts.py --denylist printed no list")
    rc, out = tools.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS]
    )
    try:
        pr = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        pr = None
    if not isinstance(pr, dict):
        return refuse(f"cannot read the PR from GitHub (gh exit {rc})")
    reasons = refusals(pr, denied)
    if reasons:
        return refuse("; ".join(reasons))
    head = pr["headRefOid"]
    statuses = read_api(tools, f"repos/{repo}/commits/{head}/statuses?per_page=100")
    files = read_api(tools, f"repos/{repo}/pulls/{number}/files?per_page=100")
    events = read_api(
        tools,
        f"repos/{repo}/activity?ref=refs/heads/{pr['headRefName']}&per_page=100",
    )
    if statuses is None or files is None or events is None:
        return refuse("cannot read the head's statuses, files or branch activity")
    soak = soak_problem(statuses)
    if soak:
        return refuse(soak)
    try:
        floor = read_floor(floor_file)
    except (OSError, ValueError) as exc:
        return refuse(f"the T2 approval floor is unusable: {exc}")
    approval = approval_reasons(files, events, head, floor)
    land = os.path.join(repo_dir, "scripts", "deploy_tools", "land.sh")
    if approval:
        log(
            f"PR #{number} needs {APPROVER}'s approval of {head[:8]}: {'; '.join(approval)}"
        )
    else:
        log(f"PR #{number} passed every check; landing {head[:8]}")
    rc, out = tools.run(
        land_argv(land, number, head, bool(approval)),
        cwd=repo_dir,
        timeout=LAND_TIMEOUT_S,
    )
    print(out, flush=True)
    atomic_write(result, verdict_line(out) + "\n")
    return rc


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main(sys.argv))
