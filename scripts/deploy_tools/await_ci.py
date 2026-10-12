#!/usr/bin/env python3
"""Wait for master CI to reach a verdict on one SHA.

THE PROBLEM. After a merge a session must know whether master CI went green before it
deploys: PR CI is scoped to changed files, so a whole-tree failure can appear only after
the merge.

`deploy_logic.ci_verdict` is already the right logic and the deployer's own gate reads it.
This is a CLI over that same function against that same endpoint, so a session's verdict
and a tick's agree by construction rather than by convention.

Exit codes:
  0   the SHA is green
  1   the SHA is red
  2   the gate could not be armed (no required contexts) -- nothing was checked
  75  the wait budget elapsed with no verdict (matches deploy.sh's use of 75)

A SHA GitHub never ran CI for at all reads green through its nearest green descendant on
master; `descendant_verdict` has the rule and why it is safe (#4621).

Run: uv run python scripts/deploy_tools/await_ci.py <sha>
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser
from lib.exit_codes import CI_DISARMED
from lib.git import git, git_stdout
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES

# The deployer's verdict logic, and host_lib for the GitHub request it shares with the tick's
# gate. deploy_logic itself stays host_lib-free, so this tool names both directories.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
sys.path.insert(0, str(HOST_LIB_FILES))

from deployer_lib.deploy_logic import _CI_NO_VERDICT_CONCLUSIONS, ci_verdict
from host_lib import github_get, github_token

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
# ansible/tests/repo/test_ci_contexts_match_workflows.py pins both directions.
REQUIRED_CONTEXTS = frozenset({"prek (lint + validate + tests + secrets)"})


# A push event GitHub never delivered leaves the SHA with no check suite from Actions, for
# good: #4596's merge commit and ee451b26e both read `total_count: 0` long after their
# neighbours went green (#4621). A freshly pushed SHA also has none for its first seconds, so
# the wait reads the absence as a dropped push only once it has waited this long.
DROPPED_PUSH_GRACE_S = 180

# How many descendants one poll asks GitHub about, nearest first. Every one is a check-runs
# request against the hourly budget `fetch_check_runs` describes.
DESCENDANT_LIMIT = 10


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
    """Check-runs for one SHA.

    Authenticated through `gh auth token` when the CLI is logged in, anonymous otherwise.
    The anonymous limit is 60/hour per source IP and this poll shares it with the deployer's
    own gate on the same host: at one request per 20s for up to 900s, one landing costs 45 of
    those 60, so the second landing in an hour starved the tick's gate into deferring on a
    403. host_lib.github_token carries the numbers.
    """
    return _github_get(f"commits/{sha}/check-runs?per_page=100").get("check_runs", [])


def fetch_check_suites(sha: str) -> list[dict]:
    """Check-suites for one SHA.

    One per workflow run, present even when the run was cancelled before any job
    registered a check-run. Fetched only when no required run exists, so it costs
    nothing on the ordinary path.
    """
    return _github_get(f"commits/{sha}/check-suites?per_page=100").get(
        "check_suites", []
    )


def _github_get(path: str) -> dict:
    return github_get(
        f"repos/{CI_REPO}/{path}",
        github_token(os.environ, subprocess.run),
        user_agent="await-ci",
    )


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


def _fetch_tip() -> str:
    # This runs from the primary checkout (land.sh cd's there) and takes no git-tree lock,
    # so it can run while the 30-min timer is mid-tick. Safe: a fetch appends objects and
    # moves a remote-tracking ref under git's own per-ref lock. It touches neither HEAD nor
    # the working tree, which is what the git-tree lock exists to guard.
    git_stdout("fetch", "-q", "origin", "master")
    return git_stdout("rev-parse", "origin/master")


def _is_ancestor(a: str, b: str) -> bool:
    """Whether `a` is an ancestor of `b`, through the same wrapper `_fetch_tip` uses.

    `lib.git.git` strips every `GIT_*` variable, so an inherited `GIT_DIR` cannot redirect
    this at another repository -- the hazard `lib/git.py`'s docstring records. A raw
    `subprocess.run(["git", ...])` here read the environment's repo while `_fetch_tip` two
    functions above read `cwd`'s.
    """
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


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


def _cancelled_before_any_run_registered(
    runs: list[dict], suites: list[dict], required
) -> bool:
    """True when no required run exists and CI already finished with no verdict.

    That covers a workflow suite for the SHA that already finished with no verdict and
    zero runs. Two merges seconds apart cancel the first one's CI workflow before its
    `prek` job registers a check-run, so the check-runs list never carries a required
    name. To `_has_only_no_verdict_conclusions` that is indistinguishable from a
    freshly-pushed SHA, and the wait would sit out its whole budget with the tip green
    the entire time. The check-suite is the record that survives: `completed cancelled`
    with `latest_check_runs_count == 0`. A suite still `queued` or `in_progress` is a
    run that may yet register, and is left alone.
    """
    if any(r.get("name") in required for r in runs):
        return False
    return any(
        s.get("status") == "completed"
        and s.get("conclusion") in _CI_NO_VERDICT_CONCLUSIONS
        and not s.get("latest_check_runs_count")
        for s in suites
    )


def _no_verdict_will_ever_arrive(target: str, runs: list[dict], fetch_suites) -> bool:
    required = required_contexts()
    if _has_only_no_verdict_conclusions(runs, required):
        return True
    if any(r.get("name") in required for r in runs):
        return False
    try:
        suites = fetch_suites(target)
    except urllib.error.URLError, TimeoutError, ValueError, OSError:
        return False
    return _cancelled_before_any_run_registered(runs, suites, required)


def _descendants(sha: str) -> list[str]:
    """The commits on origin/master's ancestry path after `sha`, nearest first.

    Empty when `sha` is the tip, is not on master, or git cannot answer. Runs through the same
    `lib.git` wrapper as `_fetch_tip`, for the reason `_is_ancestor` gives.
    """
    try:
        tip = _fetch_tip()
        out = git_stdout("rev-list", "--reverse", "--ancestry-path", f"{sha}..{tip}")
    except subprocess.CalledProcessError, OSError:
        return []
    return out.split()


def _push_was_dropped(runs: list[dict], suites: list[dict], required) -> bool:
    """True when GitHub Actions has registered nothing at all for the SHA.

    Only an Actions suite counts. Another app's suite (gitguardian, renovate) says nothing
    about whether CI was triggered. The caller applies `DROPPED_PUSH_GRACE_S`, since a fresh
    push looks the same for its first seconds.
    """
    if any(r.get("name") in required for r in runs):
        return False
    return not any((s.get("app") or {}).get("slug") == "github-actions" for s in suites)


def descendant_verdict(
    sha: str, fetch=fetch_check_runs, descendants=_descendants
) -> tuple[str, str]:
    """(descendant, verdict) for the nearest descendant of `sha` whose CI has finished.

    Called only for a SHA GitHub never ran CI for. Asks about at most `DESCENDANT_LIMIT`
    commits, nearest first, skips the ones still pending, and stops at the first `pass` or
    `fail`. ("", "pending") when none has finished yet.
    """
    # DECIDED: the nearest finished descendant's verdict stands in for a merge commit GitHub
    # never ran CI for. That descendant's run tested a tree that contains every change in the
    # merge commit, which is the rule the tick already deploys by (it fast-forwards to the
    # newest green ancestor of the tip). What it does not prove is the merge commit's own
    # tree, the one land.sh deploys with `deploy.sh --at`. Nothing can prove that once the
    # push event is gone, and the alternative is a landing no re-run converges (#4621).
    # A RED one ends the walk rather than being stepped over: a later green commit may hold
    # the fix the merge commit lacks, so its green says nothing about the tree deployed.
    required = required_contexts()
    for candidate in descendants(sha)[:DESCENDANT_LIMIT]:
        verdict = verdict_for(candidate, fetch=fetch, required=required)
        if verdict != "pending":
            return candidate, verdict
    return "", "pending"


def wait(
    sha: str,
    timeout_s: int,
    interval_s: int,
    sleep=time.sleep,
    clock=time.monotonic,
    fetch=fetch_check_runs,
    fetch_suites=fetch_check_suites,
    descendants=_descendants,
) -> tuple[int, str]:
    """(exit code, message). Polls until pass/fail or the budget elapses.

    A SHA with no Actions suite after `DROPPED_PUSH_GRACE_S` is a push GitHub dropped, and
    its verdict comes from `descendant_verdict` instead. Until one goes green it keeps waiting,
    and a timeout says why.
    """
    started = clock()
    deadline = started + timeout_s
    target = sha
    dropped = False
    while True:
        try:
            runs = fetch(target)
        except urllib.error.URLError, TimeoutError, ValueError, OSError:
            runs = []
        # `runs` is bound as a default rather than closed over: verdict_for calls this
        # synchronously within the iteration, so late binding is harmless today, but a
        # future caller that deferred the call would silently read the next poll's runs.
        verdict = verdict_for(target, fetch=lambda _s, _runs=runs: _runs)
        if verdict == "pass":
            return 0, f"{target[:8]}: CI green"
        if verdict == "fail":
            return 1, f"{target[:8]}: CI RED"
        if _no_verdict_will_ever_arrive(target, runs, fetch_suites):
            moved = resolve_sha(target)
            if moved != target:
                print(
                    f"{target[:8]} has no verdict (cancelled/stale) — "
                    f"following the tip {moved[:8]}"
                )
                target = moved
                continue
        now = clock()
        if now - started >= DROPPED_PUSH_GRACE_S and not dropped:
            try:
                dropped = _push_was_dropped(
                    runs, fetch_suites(target), required_contexts()
                )
            except urllib.error.URLError, TimeoutError, ValueError, OSError:
                dropped = False
        if any(r.get("name") in required_contexts() for r in runs):
            # A push that was only late, not dropped: its own run now carries the verdict.
            dropped = False
        if dropped:
            nearest, verdict = descendant_verdict(
                target, fetch=fetch, descendants=descendants
            )
            if verdict == "pass":
                return 0, (
                    f"{nearest[:8]}: CI green (GitHub never ran CI for {target[:8]}; "
                    f"{nearest[:8]} descends from it)"
                )
            if verdict == "fail":
                return 1, (
                    f"{nearest[:8]}: CI RED (GitHub never ran CI for {target[:8]}; "
                    f"{nearest[:8]} is its nearest descendant with a verdict)"
                )
        if now >= deadline:
            if dropped:
                return 75, (
                    f"{target[:8]}: no verdict after {timeout_s}s — GitHub never ran CI "
                    "for it, and no later master commit is green yet; a re-run converges "
                    "once one is"
                )
            return 75, f"{target[:8]}: no verdict after {timeout_s}s"
        sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    """Parse args, wait for the SHA's CI verdict, and print it.

    Exits with the code `wait` returns (see the module docstring's Exit codes), or 2
    when the required-context gate is disarmed.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
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
        return CI_DISARMED
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
