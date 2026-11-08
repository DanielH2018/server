"""The review pipeline's CI wait: `cc-wait gh-ci` run by the pipeline before its PR lands.

WHY (#4737). A daniel-server batch stops at its open PR, and the orchestrator lands it. A check
that runs only in CI, or in a suite the agent did not run, therefore first showed up as
`pr-ci-red` at the orchestrator's `land.sh` (#4727, #4736, #4753, #4869), and the orchestrator
repaired the branch by hand. The daniel-box pipeline had the same gap one step later (#5498):
`review_land` runs `land.sh`, which waits for CI but ends `pr-ci-red` on a red run, and
`RESUME_VERDICTS` does not resume for it. This module gives both hosts the same two steps, a
wait and one resume, after the review and its fix round. On daniel-box they run before
`land.sh`.

THE DRAFT (#5563). This repo's review batch opens its PR as a draft, and `ci.yml` runs no job on
a draft. `ready_gate` marks the PR ready once the review and its fix round are done. It then
waits for the run that marking ready starts, before any wait reads the head's checks.

THE WAIT is `cc-wait gh-ci --pr <n>`, the waiter every other wait in this repo uses. It reads
every check run on the PR's current head, so a push from the resume moves the wait to the new
commit. It exits 0 for a pass, 1 for a failed or cancelled run, 75 when its slice of the budget
ends, and prints the state on its last `WAIT:` line. Exit 1 does not say which of failed or
cancelled, so the line decides.

NO VERDICT. A `cancelled` conclusion means GitHub stopped the run, not that the change is bad
(`docs/landing.md`; `deploy_git._CI_NO_VERDICT_CONCLUSIONS` lists the conclusions). A cancelled
run, a failed read and a slice that ended all read the same here: wait again after
`CI_RECHECK_S`, until `CI_WAIT_S` is spent. A wait that spends it without a verdict ends the
batch `needs input:` with the last `WAIT:` line. It never reads as a pass or as red.

THE BUDGET, `CI_WAIT_S`, applies to each wait, and a batch makes at most two: one before the
resume and one after it. On 2026-10-13 the last 34 `ci.yml` pull-request runs that finished
with success or failure took 143s at the shortest, 176s at the median and 344s at the longest.
That sized the first budget at 15 minutes. On 2026-10-24 and 2026-10-29 a fan-out launching
several batches at once slowed the same runs: of the 40 finished runs among the 60 newest on
2026-10-29, 12 took 1089s to 1404s (runs 40893690313, 40923262400, 40913469512, 40921080107,
40914146732, 40908876093, 40919912025, 40896956629, 40892178310 and 40903936455 among them),
and PR #6185's run 40134967214 took 16m18s on 2026-10-24. The 15-minute wait spent its whole
budget and ended `needs input:` on green CI (#6195). The last line of such a wait reads
"timeout still running after 300s" because the budget is three 300s slices. It is not a
single slice. 35 minutes is about 1.5 times the longest run measured, 1404s.

A wait whose last line reads `timeout still running` goes on to `CI_WAIT_MAX_S` (#7673). That
line means check runs exist on the head and some have not finished. Between 2026-10-29 and
2026-11-08, three batches spent the 35 minutes on such a line, and their PRs' runs then passed
at 37.8, 38.2 and 48.5 minutes (#7059, #7118, #7658). A head with no check runs, or a cancelled
run, still stops at `CI_WAIT_S`.

THE RE-RUN (#7674). A resume that pushes nothing leaves the failed run on the head, so the
second wait would read the same failure at once. The gate instead re-runs that run's failed jobs
once (`gh run rerun --failed`) and waits on the new attempt. Of the 8 batches that ended `failed
after one resume` between 2026-10-25 and 2026-11-08, 4 had pushed nothing and passed on a
re-run of the same commit (#7110, #7169, #7643, #7647).

THE FAILURE the resumed implementer gets is read from the PR's `statusCheckRollup`: each failed
check's name and the tail of its job's log (`gh run view --job <id> --log-failed`). A failed
check with no job log, such as an external app's, is named with an empty tail.
"""

import json
import math
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.review.review_land import WAIT_MARGIN_S, login
from fanout_lib.review.review_prompts import ci_fix_prompt
from fanout_lib.review.review_record import Phase, Record
from fanout_lib.brief import drafts_pr
from fanout_lib.status import PR_URL
from fanout_lib.target import Target

if TYPE_CHECKING:
    from fanout_lib.review.review import Pipeline

# The longest one wait lasts. Derived from measured `ci.yml` run durations in the docstring.
CI_WAIT_S = 35 * 60
# The longest one wait lasts while the head's check runs are still running.
CI_WAIT_MAX_S = 60 * 60
STILL_RUNNING = re.compile(r"^WAIT: timeout still running\b")
# One `cc-wait` call lasts at most this long, so the wait rechecks the unit's time left.
CI_SLICE_S = 300
# The pause after a wait that ended without a verdict and without using its slice.
CI_RECHECK_S = 60
TAIL_LINES = 40
# The cap on one failed job's log in the resume prompt.
LOG_CHARS = 4000
MAX_FAILING = 8
# How long `ready_for_ci` waits for the run `gh pr ready` starts. GitHub registers a run within
# seconds of the event, so two minutes is a queue that is stuck, not slow.
READY_WAIT_S = 120
READY_POLL_S = 10
# GitHub reports `UNKNOWN` for a PR's `mergeable` while it computes it, so a read of `UNKNOWN`
# is repeated once after this pause.
MERGEABLE_RECHECK_S = 5
# The conclusions cc-wait's `gh-ci` source fails on (`cc_wait/github.py:FAILED_CONCLUSIONS`),
# and `error`, which a commit status reports. `cancelled` is deliberately absent: it is no
# verdict.
FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure", "error"}
)
PASSED = "passed"
FAILED = "failed"
NO_VERDICT = "no-verdict"
WAIT_LINE = re.compile(r"(?m)^WAIT: (\S+)(.*)$")
JOB_ID = re.compile(r"/job/(\d+)")

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


@dataclass
class Failure:
    """One failed check: its name and the end of its job's log."""

    name: str
    tail: str = ""


@dataclass
class CiResult:
    """What one CI wait found.

    Attributes:
        state: `PASSED`, `FAILED` or `NO_VERDICT`.
        line: the last `WAIT:` line cc-wait printed, "" when it printed none.
        failures: the failed checks, for `FAILED`.
        waited: the seconds the wait spent, all slices and pauses together.
    """

    state: str
    line: str = ""
    failures: list[Failure] = field(default_factory=list)
    waited: float = 0.0

    def names(self) -> str:
        return ", ".join(f.name for f in self.failures) or "an unnamed check"


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])[-LOG_CHARS:]


def failing_checks(run: Runner, pr: str, repo: str) -> list[Failure]:
    """The PR head's failed checks with each job's log tail; empty when the read fails."""
    out = run(["gh", "pr", "view", pr, "-R", repo, "--json", "statusCheckRollup"], None)
    try:
        rollup = json.loads(out.stdout)["statusCheckRollup"]
    except ValueError, KeyError, TypeError:
        return []
    failures = []
    for check in rollup if isinstance(rollup, list) else []:
        if not isinstance(check, dict):
            continue
        # A check run reports `conclusion`; a commit status reports `state`.
        verdict = str(check.get("conclusion") or check.get("state") or "").lower()
        if verdict not in FAILED_CONCLUSIONS:
            continue
        name = str(check.get("name") or check.get("context") or "unnamed")
        url = str(check.get("detailsUrl") or check.get("targetUrl") or "")
        job = JOB_ID.search(url)
        tail = ""
        if job:
            log = run(
                [
                    "gh",
                    "run",
                    "view",
                    "--job",
                    job.group(1),
                    "-R",
                    repo,
                    "--log-failed",
                ],
                None,
            )
            tail = _tail(log.stdout)
        failures.append(Failure(name, tail))
    return failures[:MAX_FAILING]


def wait_for_ci(
    run: Runner,
    pr_url: str,
    repo: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    time_left: Callable[[], float],
) -> CiResult:
    """Wait up to `CI_WAIT_S` for the PR head's CI verdict, or `CI_WAIT_MAX_S` while it runs.

    A pass or a failure ends the wait at once. Anything else, a cancelled run included, is
    waited on again until the budget is spent, and then returns `NO_VERDICT`. The budget is
    `CI_WAIT_MAX_S` while the last `WAIT:` line reads `timeout still running`. A budget cut
    short by `time_left()` is the same.
    """
    pr = pr_url.rstrip("/").rsplit("/", 1)[-1]
    start = clock()
    cap = time_left() - WAIT_MARGIN_S
    soft = start + min(CI_WAIT_S, cap)
    hard = start + min(CI_WAIT_MAX_S, cap)
    line = ""
    while (left := (hard if STILL_RUNNING.match(line) else soft) - clock()) > 0:
        budget = str(min(CI_SLICE_S, math.ceil(left)))
        wait = ["cc-wait", "--budget", budget, "gh-ci", "--pr", pr, "--repo", repo]
        done = run(login(wait), None)
        found = list(WAIT_LINE.finditer(done.stdout))
        line = found[-1].group(0) if found else line
        state = found[-1].group(1) if found else ""
        if done.returncode == 0:
            return CiResult(PASSED, line, waited=clock() - start)
        if done.returncode == 1 and state == "failed":
            failures = failing_checks(run, pr, repo)
            return CiResult(FAILED, line, failures, clock() - start)
        # Exit 75 used its slice. Any other end, `cancelled` among them, returned at once.
        if done.returncode != 75:
            sleep(CI_RECHECK_S)
    return CiResult(NO_VERDICT, line, waited=clock() - start)


def _ci_run_ids(run: Runner, repo: str, sha: str) -> set[int] | None:
    """The ids of the `ci.yml` pull-request runs on `sha`, or None when the read fails."""
    out = run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&event=pull_request",
            "--jq",
            "[.workflow_runs[].id]",
        ],
        None,
    )
    try:
        ids = json.loads(out.stdout) if out.returncode == 0 else None
    except ValueError:
        return None
    return {i for i in ids if isinstance(i, int)} if isinstance(ids, list) else None


def pr_head(run: Runner, pr: str, repo: str) -> str:
    """The PR's head commit, "" when the read fails."""
    out = run(["gh", "pr", "view", pr, "-R", repo, "--json", "headRefOid"], None)
    try:
        return str(json.loads(out.stdout).get("headRefOid") or "")
    except ValueError, AttributeError:
        return ""


def rerun_failed(
    run: Runner,
    repo: str,
    sha: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Re-run the failed jobs of the failed `ci.yml` run on `sha`, and wait for the new attempt.

    Returns True once GitHub reports the run's next attempt. Returns False when no failed run
    is on `sha`, when `gh run rerun` fails, or when no new attempt registers within
    `READY_WAIT_S`. Until it registers, the head still carries the failed check runs, and a
    wait started then would read the old failure.
    """
    out = run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/ci.yml/runs"
            f"?head_sha={sha}&event=pull_request&status=failure",
            "--jq",
            "[.workflow_runs[] | [.id, .run_attempt]]",
        ],
        None,
    )
    try:
        found = json.loads(out.stdout) if out.returncode == 0 else []
    except ValueError:
        return False
    runs = [
        (r[0], r[1])
        for r in (found if isinstance(found, list) else [])
        if isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r)
    ]
    if not runs:
        return False
    run_id, attempt = max(runs)
    rerun = run(["gh", "run", "rerun", str(run_id), "--failed", "-R", repo], None)
    if rerun.returncode != 0:
        return False
    end = clock() + READY_WAIT_S
    while clock() < end:
        now = run(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runs/{run_id}",
                "--jq",
                ".run_attempt",
            ],
            None,
        )
        if now.returncode == 0 and now.stdout.strip().isdigit():
            if int(now.stdout) > attempt:
                return True
        sleep(READY_POLL_S)
    return False


def _mergeable(run: Runner, pr: str, repo: str, sleep: Callable[[float], None]) -> str:
    """The PR's `mergeable` state (`CONFLICTING`, `MERGEABLE`, ...), "" when unreadable.

    One `UNKNOWN` read is repeated after `MERGEABLE_RECHECK_S`, since GitHub is still computing.
    """
    state = ""
    for attempt in range(2):
        out = run(["gh", "pr", "view", pr, "-R", repo, "--json", "mergeable"], None)
        try:
            state = str(json.loads(out.stdout)["mergeable"]).upper()
        except ValueError, KeyError, TypeError:
            return ""
        if state != "UNKNOWN":
            break
        if attempt == 0:
            sleep(MERGEABLE_RECHECK_S)
    return state


def ready_for_ci(
    run: Runner,
    pr_url: str,
    repo: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    """Mark the draft PR ready for review, then wait for the `ci.yml` run that starts.

    Returns "" once that run exists, or when the PR was not a draft. Otherwise returns why
    CI cannot be trusted to have started, which the caller turns into `needs input:`.

    The wait is the guard. A draft run skips every job, the `prek` gate included. Both
    `cc-wait gh-ci` and `land.sh`'s `await_ci` count a skipped check run as a pass. For the
    seconds before the ready run registers, the draft run's skipped checks are therefore the
    only ones on the head, and a wait started then would pass a head that nothing tested.
    A run id that was absent before `gh pr ready` is the ready run, because the draft run on
    the same head registered minutes earlier, when the implementer or the fix round pushed.
    """
    pr = pr_url.rstrip("/").rsplit("/", 1)[-1]
    out = run(
        ["gh", "pr", "view", pr, "-R", repo, "--json", "isDraft,headRefOid"], None
    )
    try:
        view = json.loads(out.stdout)
    except ValueError:
        view = None
    if not isinstance(view, dict) or "isDraft" not in view:
        return f"`gh pr view {pr}` gave no draft state, so the PR may still be a draft"
    if view["isDraft"] is not True:
        return ""
    sha = str(view.get("headRefOid") or "")
    before = _ci_run_ids(run, repo, sha) if sha else None
    if before is None:
        return f"could not list the CI runs on PR #{pr}'s head before marking it ready"
    ready = run(["gh", "pr", "ready", pr, "-R", repo], None)
    if ready.returncode != 0:
        why = (ready.stderr or "").strip() or f"exit {ready.returncode}"
        return (
            f"`gh pr ready {pr}` failed ({why}), so the PR is still a draft with no CI"
        )
    end = clock() + READY_WAIT_S
    while clock() < end:
        now = _ci_run_ids(run, repo, sha)
        if now is not None and now - before:
            return ""
        sleep(READY_POLL_S)
    # GitHub starts no `pull_request` run while the PR conflicts with its base (#5629), so a
    # conflict, not Actions, is why none started. The PR stays ready: GitHub cannot merge it
    # while it conflicts, and the push that resolves the conflict starts CI with no second
    # `gh pr ready`.
    if _mergeable(run, pr, repo, sleep) == "CONFLICTING":
        return (
            f"PR #{pr} conflicts with master, so marking it ready started no CI on head "
            f"{sha[:8]}. Merge or rebase master into the branch and push; the push starts CI"
        )
    # A ready PR whose only checks are the draft run's skipped ones reads green to GitHub's
    # ruleset and to auto-merge, so the PR goes back to a draft, which GitHub refuses to merge.
    undo = run(["gh", "pr", "ready", pr, "-R", repo, "--undo"], None)
    state = "back to a draft" if undo.returncode == 0 else "still ready, NOT a draft"
    return (
        f"no CI run started on PR #{pr}'s head {sha[:8]} within {READY_WAIT_S}s of "
        f"marking it ready; the PR is {state}. Run `gh pr ready {pr}` once Actions is up"
    )


def ready_gate(pipe: "Pipeline") -> dict | None:
    """Mark this repo's draft PR ready (`ready_for_ci`); a `needs input:` report if it fails."""
    if not drafts_pr(pipe.target):
        return None
    why = ready_for_ci(
        pipe.run, pipe.record.pr, pipe.target.repo, pipe.clock, pipe.sleep
    )
    if not why:
        return None
    # `status` reads any `https://…/pull/N` in the text as `done`, so the hold names the PR
    # without its scheme.
    pr = pipe.record.pr.removeprefix("https://")
    return {"type": "result", "is_error": False, "result": f"needs input: {why}.\n{pr}"}


def awaits_ci(target: Target) -> bool:
    """Whether a batch waits for its PR's CI: any batch in this repo, on either host.

    On the deploy host the wait runs before `review_land` starts `land.sh` (#5498). Left to
    `land.sh`, a red CI ended `pr-ci-red` and the batch `needs-input`, 9 of 24 box
    `needs-input` outcomes over 2026-10-15 to 2026-10-17. A green PR's CI is waited on
    either way, so the extra wait falls on red runs only. Another repo's batch is left alone:
    this module knows nothing of that repo's CI, and a repo with no CI would wait out the
    whole budget.
    """
    return target.is_server


# DECIDED: the CI resume gets no delta review. It repairs a check CI named, and the next CI run
# is the independent verdict on it.
def ci_gate(pipe: "Pipeline", last: Phase) -> tuple[Phase, dict | None]:
    """Wait for the PR's CI and resume the implementer once on red (#4737).

    A resume that pushes nothing is followed by one re-run of the failed jobs (#7674).

    Returns the last phase whose report names the PR, and a `needs input:` report when CI is
    still red after the one resume, or gave no verdict within the budget; else None.
    """
    record = pipe.record

    def wait() -> CiResult:
        return wait_for_ci(
            pipe.run,
            record.pr,
            pipe.target.repo,
            pipe.clock,
            pipe.sleep,
            lambda: pipe.deadline - pipe.clock(),
        )

    result = wait()
    # After an upheld contest, the red test CI fails on is wrong, and a resume that rewrote it
    # would face no green gate and no reviewer. A hand corrects it instead (#5400).
    contest = record.green_contest.removeprefix("upheld: ")
    if contest != record.green_contest:
        return last, ci_hold(record, result, contest)
    if result.state == FAILED and pipe.session:
        record.ci_resumed = True
        repo = pipe.target.repo
        before = pr_head(pipe.run, record.pr, repo)
        fix = pipe._claude(
            "ci-fix",
            pipe._resume(),
            ci_fix_prompt(record.pr, result.line, result.failures),
        )
        last = fix if PR_URL.search(fix.text) else last
        if before and pr_head(pipe.run, record.pr, repo) == before:
            record.ci_rerun = rerun_failed(
                pipe.run, repo, before, pipe.clock, pipe.sleep
            )
        result = wait()
    again = " after one resume" if record.ci_resumed else ""
    again += " and a re-run of the failed jobs" if record.ci_rerun else ""
    if result.state == PASSED:
        record.ci_gate = f"passed{again}"
        return last, None
    if result.state == FAILED:
        record.ci_gate = f"failed{again}: {result.names()}"
        why = f"CI is red{again}, so the PR needs a hand: {result.names()}"
    else:
        detail = result.line or "cc-wait printed no state"
        spent = f"{result.waited / 60:.0f} min"
        record.ci_gate = f"no verdict{again}: {detail} (waited {spent})"
        why = (
            f"CI gave no verdict{again} within its wait of {spent} ({detail}). "
            f"Check the PR's runs, "
            f"and re-run `cc-wait gh-ci --pr <n> --repo {pipe.target.repo}`"
        )
    return last, _needs_input(record, why)


def ci_hold(record: Record, result: "CiResult", contest: str) -> dict | None:
    """A hold on a PR whose CI is not green after an upheld contest, or None when it is green."""
    if result.state == PASSED:
        record.ci_gate = "passed"
        return None
    record.ci_gate = f"held after an upheld contest: {result.line or 'no state'}"
    why = (
        f"CI is not green on a PR whose contest of {contest} a fresh reviewer upheld. Correct "
        f"those red tests to the cited fact by hand, then land it: {result.line or 'no state'}"
    )
    return _needs_input(record, why)


def _needs_input(record: Record, why: str) -> dict:
    # `status` reads any `https://…/pull/N` in the text as `done`, so the hold names the PR
    # without its scheme.
    pr = record.pr.removeprefix("https://")
    return {"type": "result", "is_error": False, "result": f"needs input: {why}.\n{pr}"}
