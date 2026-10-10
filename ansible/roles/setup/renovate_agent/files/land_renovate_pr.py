#!/usr/bin/env python3
"""Land one Renovate PR for the unattended agent, on checks the agent cannot influence.

The agent's session runs as its own Unix user on a repo-scoped GitHub token that cannot write
repository contents, so it cannot merge. It asks for a landing by starting
`renovate-agent-land@<n>.service`, which polkit lets that user start and which runs this as the
operator's user. Every input here is read from GitHub with the operator's token or from the
deployer's own rendered config. Nothing comes from a file the session can write.

A PR is landed only when all of these hold:

- GitHub names `app/renovate` as its author, which no edit to the PR can change.
- Its head branch is in this repository and named `renovate/...`, and its base is `master`.
  A token with pull-request write can retarget a base, so the base is checked too.
- It is open.
- Its branch carries no `k8s_autodeploy-false` slug, and no changed path sits under a k8s role
  the deployer's denylist names. The title is never read, because the session can edit it.
- It changes nothing under `ansible/inventory/`. A pin there, such as `crowdsec_k8s_image` in
  `group_vars/all.yml`, reaches every role that reads it, denylisted ones included, and no path
  check can see which those are. An interactive session lands that class.
- Its `renovate/stability-days` status, when it carries one, reads success. GitHub's auto-merge
  waits only on the required checks, so without this a PR forced open mid-soak would land.

Then `land.sh` runs as an interactive session would run it, and only its `VERDICT:` line is
written where the session can read it. Stdlib only, like the rest of the agent.

Usage: land_renovate_pr.py <pr-number>

Started as `renovate-agent-land@<pr-number>.service`, never by hand. Reads the PR with
`gh`, the deployer's config at /etc/gitops-deploy/config.env, and the environment
RENOVATE_AGENT_REPO (default DanielH2018/server) and RENOVATE_AGENT_REPO_DIR (default
/home/ubuntu/server). Writes the outcome to /var/lib/renovate-agent-land/<pr-number>.verdict.
Exit codes: land.sh's own on a landing, 2 for an argument that is not a PR number, 3 for a
refusal.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import TOOLS, AgentTools, log
from host_lib import atomic_write, parse_env_file

AUTHOR = "app/renovate"
BASE = "master"
BRANCH_PREFIX = "renovate/"
DENIED_SLUG = "k8s_autodeploy-false"
DEPLOYER_CONFIG = "/etc/gitops-deploy/config.env"
RESULTS_DIR = "/var/lib/renovate-agent-land"
# gh returns at most 100 changed files, so a PR at the cap may hide a denied path past it.
FILE_CAP = 100
# land.sh waits on the PR's CI, the merge, master CI, the tick, the deploy and the health gate.
# The unit's TimeoutStartSec sits above this, so the script reports a timeout before systemd
# kills it mid-deploy.
LAND_TIMEOUT_S = 3300
PR_FIELDS = (
    "author,isCrossRepository,baseRefName,state,headRefName,files,statusCheckRollup"
)
SOAK_CONTEXT = "renovate/stability-days"
INVENTORY = "ansible/inventory/"
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,6}")
_K8S_ROLE = re.compile(r"^ansible/roles/k8s/([^/]+)/")
_VERDICT = re.compile(r"^VERDICT: .*$", re.MULTILINE)


def pr_number(arg: str) -> int | None:
    """`arg` as a PR number, or None for anything but a plain positive integer."""
    return int(arg) if _PR_NUMBER.fullmatch(arg) else None


def denied_roles(deployer_env: dict[str, str]) -> frozenset[str] | None:
    """The k8s roles the deployer's rendered denylist names, or None when it names none.

    An empty list does not mean "nothing is denied". The deployer disarms auto-deploy on an
    empty denylist for that reason, so this refuses rather than landing past it.
    """
    listed = deployer_env.get("K8S_AUTODEPLOY_DENYLIST", "").split(",")
    return frozenset(role for role in listed if role) or None


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
    soak = [
        check.get("state")
        for check in pr.get("statusCheckRollup") or []
        if check.get("context") == SOAK_CONTEXT
    ]
    if any(state != "SUCCESS" for state in soak):
        reasons.append(f"its {SOAK_CONTEXT} status has not passed")
    paths = sorted({f.get("path", "") for f in pr.get("files") or []})
    if any(path.startswith(INVENTORY) for path in paths):
        reasons.append(f"it changes {INVENTORY}, which can reach a denylisted role")
    if not paths or len(paths) >= FILE_CAP:
        reasons.append(f"its changed-file list is empty or at gh's {FILE_CAP}-file cap")
    for path in paths:
        match = _K8S_ROLE.match(path)
        if match and match.group(1) in denied:
            reasons.append(
                f"{path} is in {match.group(1)}, which the deployer denylists"
            )
    return reasons


def verdict_line(output: str) -> str:
    """The last `VERDICT:` line land.sh printed, or a stand-in when it printed none."""
    found = _VERDICT.findall(output)
    return found[-1] if found else "VERDICT: none (land.sh printed no verdict line)"


def main(
    argv: list[str],
    tools: AgentTools = TOOLS,
    deployer_config: str = DEPLOYER_CONFIG,
    results_dir: str = RESULTS_DIR,
) -> int:
    """Check PR `argv[1]`, land it if every check passes, and record the outcome.

    Returns land.sh's exit code, 2 for a malformed argument and 3 for a refusal. The result
    file is overwritten before anything else runs, so the session never reads an earlier
    landing's verdict as this one's.
    """
    number = pr_number(argv[1]) if len(argv) == 2 else None
    if number is None:
        log(f"refused: {argv[1:]!r} is not a PR number")
        return 2
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

    try:
        denied = denied_roles(parse_env_file(deployer_config))
    except OSError as e:
        return refuse(f"cannot read the deployer's denylist ({e})")
    if denied is None:
        return refuse("the deployer's denylist is empty")
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
    land = os.path.join(repo_dir, "scripts", "deploy_tools", "land.sh")
    log(f"PR #{number} passed every check; landing it")
    rc, out = tools.run(
        [land, "--pr", str(number), "--arm-merge", "--await-merge"],
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
