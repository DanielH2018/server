#!/usr/bin/env python3
"""Wait for master CI to reach a verdict on one SHA.

THE PROBLEM. After a merge a session must know whether master CI went green before it
deploys: PR CI is scoped to changed files, so a whole-tree failure can appear only after
the merge. Nothing exposed that wait, so sessions hand-polled -- 835 polls across 213 wait
episodes, measured 2026-08-29.

`deploy_logic.ci_verdict` is already the right logic and the deployer's own gate reads it.
This is a CLI over that same function against that same endpoint, so a session's verdict
and a tick's agree by construction rather than by convention.

Exit codes:
  0   the SHA is green
  1   the SHA is red
  2   the gate could not be armed (no required contexts) -- nothing was checked
  75  the wait budget elapsed with no verdict (matches deploy.sh's use of 75)

Run: uv run python scripts/deploy_tools/await_ci.py <sha>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(
    0, str(_REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files")
)

from deploy_logic import (  # noqa: E402 — needs the path insert above
    _CI_NO_VERDICT_CONCLUSIONS,
    ci_verdict,
)

CI_REPO = "DanielH2018/server"

# The check-run NAMES that must be green -- GitHub's own `name:` values, which must match
# .github/workflows/ci.yml exactly, since a name matching nothing silently drops a required
# check.
#
# This must equal the deployer's gate, which is `gitops_deploy_ci_contexts` in
# roles/setup/gitops_deploy/defaults/main.yml (rendered to CI_CONTEXTS in config.env, a
# root-owned file on daniel-box that a worktree session cannot read). Equal in BOTH
# directions: a name here the deployer does not require blocks a landing the deployer would
# have deployed. `renovate config validator` is the live instance -- it runs on master and
# can go red, and the deployer deliberately excludes it because a red renovate.json changes
# nothing a deploy renders. Requiring it here would have parked every landing behind a
# dependency-management fault.
# ansible/tests/test_ci_contexts_match_workflows.py pins both directions.
REQUIRED_CONTEXTS = frozenset({"prek (lint + validate + tests + secrets)"})


class DisarmedGateError(RuntimeError):
    """Raised when the required-context set is empty.

    `ci_verdict` returns `pass` for an empty set (deploy_logic.py:366). That is the
    deployer's deliberate disarm switch, reachable only by an operator emptying
    CI_CONTEXTS. Inheriting it here would turn a wait-for-green into an unconditional
    green, so this refuses instead of returning a verdict it did not check.
    """


def required_contexts() -> frozenset[str]:
    return REQUIRED_CONTEXTS


def fetch_check_runs(sha: str) -> list[dict]:
    """Check-runs for one SHA. Unauthenticated -- the repo is public."""
    url = (
        f"https://api.github.com/repos/{CI_REPO}/commits/{sha}/check-runs?per_page=100"
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "await-ci"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp).get("check_runs", [])


def verdict_for(sha: str, fetch=fetch_check_runs, required=None) -> str:
    """`pass` / `pending` / `fail` for one SHA. An unreachable API reads as pending."""
    required = required_contexts() if required is None else required
    if not required:
        raise DisarmedGateError(
            "no required CI contexts -- refusing to report a verdict"
        )
    try:
        runs = fetch(sha)
    except urllib.error.URLError, TimeoutError, ValueError, OSError:
        return "pending"
    return ci_verdict(runs, required)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _fetch_tip() -> str:
    # This runs from the primary checkout (land.sh cd's there) and takes no git-tree lock,
    # so it can run while the 30-min timer is mid-tick. Safe: a fetch appends objects and
    # moves a remote-tracking ref under git's own per-ref lock. It touches neither HEAD nor
    # the working tree, which is what /var/lock/server-git-tree.lock exists to guard.
    _git("fetch", "-q", "origin", "master")
    return _git("rev-parse", "origin/master")


def _is_ancestor(a: str, b: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, b], capture_output=True
        ).returncode
        == 0
    )


def resolve_sha(sha: str, fetch_tip=_fetch_tip, is_ancestor=_is_ancestor) -> str:
    """The SHA whose check-runs actually carry a verdict for `sha`.

    Two merges in quick succession cancel the first run, so a commit whose merge was
    immediately followed by another reads `completed cancelled` permanently and polling it
    never terminates. `cancelled`, `stale` and `skipped_by_concurrency` mean NO VERDICT for
    this SHA, not that it is bad.

    The verdict then comes from the tip -- but only once `sha` is an ancestor of it.
    An unrelated tip says nothing about this commit, and following one would report
    somebody else's result as this PR's.
    """
    try:
        tip = fetch_tip()
    except subprocess.CalledProcessError, OSError:
        return sha
    if tip != sha and is_ancestor(sha, tip):
        return tip
    return sha


def _has_only_no_verdict_conclusions(runs: list[dict], required) -> bool:
    """True when every required run that exists finished with no verdict.

    This is the trigger for following the tip. It deliberately returns False when no
    required run exists at all: that is a freshly-pushed SHA whose workflow has not
    registered, which resolves on its own and must not send the wait chasing the tip.
    """
    seen = [r for r in runs if r.get("name") in required]
    if not seen:
        return False
    return all(
        r.get("status") == "completed"
        and r.get("conclusion") in _CI_NO_VERDICT_CONCLUSIONS
        for r in seen
    )


def wait(
    sha: str,
    timeout_s: int,
    interval_s: int,
    sleep=time.sleep,
    clock=time.monotonic,
    fetch=fetch_check_runs,
) -> tuple[int, str]:
    """(exit code, message). Polls until pass/fail or the budget elapses."""
    deadline = clock() + timeout_s
    target = sha
    while True:
        try:
            runs = fetch(target)
        except urllib.error.URLError, TimeoutError, ValueError, OSError:
            runs = []
        verdict = verdict_for(target, fetch=lambda _s: runs)
        if verdict == "pass":
            return 0, f"{target[:8]}: CI green"
        if verdict == "fail":
            return 1, f"{target[:8]}: CI RED"
        if _has_only_no_verdict_conclusions(runs, required_contexts()):
            moved = resolve_sha(target)
            if moved != target:
                print(
                    f"{target[:8]} has no verdict (cancelled/stale) — "
                    f"following the tip {moved[:8]}"
                )
                target = moved
                continue
        if clock() >= deadline:
            return 75, f"{target[:8]}: no verdict after {timeout_s}s"
        sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="the commit to wait on")
    parser.add_argument(
        "--timeout", type=int, default=900, help="wait budget in seconds"
    )
    parser.add_argument(
        "--interval", type=int, default=20, help="poll interval in seconds"
    )
    ns = parser.parse_args(argv)
    try:
        code, msg = wait(ns.sha, ns.timeout, ns.interval)
    except DisarmedGateError as e:
        print(f"await_ci: {e}", file=sys.stderr)
        return 2
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
