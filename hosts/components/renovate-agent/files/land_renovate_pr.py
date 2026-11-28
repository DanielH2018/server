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
- It changes no shared variables file: nothing under `estate/`, and no
  `k8s/vars.yml`, `hosts/vars.yml` or `hosts/<host>/vars.yml`. A pin there, such as
  `crowdsec_k8s_image`, reaches every role that reads it, denylisted ones included, and no path
  check can see which those are. An interactive session lands that class.
- Its `renovate/stability-days` status, when it carries one, reads success and was posted by
  `renovate[bot]`. GitHub's auto-merge waits only on the required checks, so without this a PR
  forced open mid-soak would land, and DanielClaudeBot's write access can post a status.

Then `land.sh` runs as an interactive session would run it, pinned to the head SHA these checks
read, and only its `VERDICT:` line is written where the session can read it. land.sh also needs
the operator's approval of that head when the PR changes a path on the T2 approval floor
other than by a digest-only image bump, or when anyone but `renovate[bot]` pushed to its branch.
Stdlib only, like the rest of the agent.

Usage: land_renovate_pr.py <pr-number>

Started as `renovate-agent-land@<pr-number>.service`, never by hand. Reads the PR with
`gh`, the denylist from `scripts/dev/k8s_autodeploy_counts.py --denylist` in the checkout, and
the environment RENOVATE_AGENT_REPO (default DanielH2018/server) and RENOVATE_AGENT_REPO_DIR
(default /home/ubuntu/server). Writes the outcome to
/var/lib/renovate-agent-land/<pr-number>.verdict.
Exit codes: land.sh's own on a landing, 64 for an argument that is not a PR number, 3 for a
refusal before any merge: the lander's own, or a land.sh `refused` on a PR GitHub reports
unmerged. A land.sh crash also prints `refused`, but its traceback keeps land.sh's exit 1. The unit lists 3 in `SuccessExitStatus=`, so a refusal leaves it inactive rather than
failed and `SystemdUnitsFailed` pages only for a landing that went wrong.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import TOOLS, AgentTools, log
from ci_verdict import parse_commit_statuses
from host_lib import atomic_write
from renovate_floor import (
    FLOOR_FILE,
    floor_hits,
    pusher_problem,
    read_floor,
    soak_problem,
)
from renovate_identity import BRANCH_PREFIX, GH_AUTHOR, PayloadError, RenovatePR

# The login whose approval of the head lets land.sh land a PR the floor check flags.
APPROVER = "DanielH2018"
BASE = "master"
DENIED_SLUG = "k8s_autodeploy-false"
# `--frozen`: a rewritten uv.lock would dirty the primary checkout, which parks
# `homelab-checkout-sync`.
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
# LAND_DEADLINE's 41460 plus the 22200 LAND_WINDOW_GRACE the reboot window may add.
LAND_TIMEOUT_S = 63660
PR_FIELDS = (
    "number,author,isCrossRepository,baseRefName,state,headRefName,headRefOid,files"
)
# ADR-0042's variables files.
VARS_FILE = re.compile(r"^(?:estate/|k8s/vars\.yml$|hosts/(?:[^/]+/)?vars\.yml$)")
# `lib/exit_codes.py:USAGE_ERROR`, which this host-shipped script cannot import.
USAGE_ERROR = 64
REFUSED_EXIT = 3
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,6}")
_SHA = re.compile(r"[0-9a-f]{40}")
_K8S_ROLE = re.compile(r"^k8s/roles/([^/]+)/")
_VERDICT = re.compile(r"^VERDICT: .*$", re.MULTILINE)
_ROLE_LIST = re.compile(r"[a-z0-9_-]+(?:,[a-z0-9_-]+)*")
# land.py prints `VERDICT: refused` for a crash too, then re-raises, so a traceback follows it.
TRACEBACK = "Traceback (most recent call last):"


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
    try:
        renovate = RenovatePR.from_gh(pr)
    except PayloadError as exc:
        return [f"GitHub's answer does not parse as a PR: {exc}"]
    reasons = []
    if not renovate.is_renovate:
        reasons.append(f"its author is not {GH_AUTHOR}")
    if pr.get("isCrossRepository") is not False:
        reasons.append("its head branch is not in this repository")
    if pr.get("baseRefName") != BASE:
        reasons.append(f"its base is not {BASE}")
    if pr.get("state") != "OPEN":
        reasons.append("it is not open")
    if not renovate.on_renovate_branch:
        reasons.append(f"its head branch is not {BRANCH_PREFIX}...")
    if DENIED_SLUG in renovate.head_ref:
        reasons.append(f"its head branch carries {DENIED_SLUG}")
    if not _SHA.fullmatch(pr.get("headRefOid") or ""):
        reasons.append("GitHub returned no head SHA to pin the landing to")
    paths = sorted({f.get("path", "") for f in pr.get("files") or []})
    if any(VARS_FILE.match(path) for path in paths):
        reasons.append("it changes a variables file, which can reach a denylisted role")
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


def unmerged(tools: AgentTools, repo: str, number: int) -> bool:
    """Whether GitHub's REST API reports PR `number` not merged; False when the read fails.

    REST, because the refusal this tells apart is often GraphQL's quota running out, and the
    two quotas are separate. land.sh merges the PR itself, so once it exits nothing merges it.
    """
    rc, out = tools.run(["gh", "api", f"repos/{repo}/pulls/{number}"])
    try:
        pr = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        return False
    return isinstance(pr, dict) and pr.get("merged") is False


def main(
    argv: list[str],
    tools: AgentTools = TOOLS,
    results_dir: str = RESULTS_DIR,
    floor_file: str = FLOOR_FILE,
) -> int:
    """Check PR `argv[1]`, land it if every check passes, and record the outcome.

    Returns land.sh's exit code, 64 for a malformed argument and `REFUSED_EXIT` for a refusal
    before any merge, land.sh's `refused` on a PR still unmerged included. The result file is
    overwritten before anything else runs, so the session never reads an earlier landing's
    verdict as this one's.
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
        return REFUSED_EXIT

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
    soak = soak_problem(parse_commit_statuses(statuses))
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
    verdict = verdict_line(out)
    atomic_write(result, verdict + "\n")
    if (
        rc != 0
        and verdict.startswith("VERDICT: refused ")
        and TRACEBACK not in out
        and unmerged(tools, repo, number)
    ):
        log(f"PR #{number} refused by land.sh before any merge")
        return REFUSED_EXIT
    return rc


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print((__doc__ or "").strip())
        sys.exit(0)
    sys.exit(main(sys.argv))
