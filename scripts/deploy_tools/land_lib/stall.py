"""What the merge wait does about a head whose CI no longer moves on its own.

await_ci names two such heads. A draft-skipped head got only a draft run, or a ready run that
the draft run cancelled, and GitHub starts no other run for the ready flip. On a no-run-left
head every check-run finished and none decided the head, as when a run is cancelled with no
skip beside it. Either head stays `pending` for good unless the landing starts a run.

`step` is the decision, with no I/O. `on_poll` hands it each poll of `await_merge` and
performs the action it returns. Once a head has stalled for `GRACE_S`, the step reads whether
the PR is ready and whether a run that can still give a verdict exists, and then:

- waits while the PR is a draft, because someone marks it ready and that starts a run;
- holds, restarting the grace, while such a run exists, because the action could cancel it;
- starts a run once per head and stall: a draft toggle for a draft-skipped head, a re-run of
  the newest cancelled run for a no-run-left head;
- gives up, which ends the landing and frees the landing turn, if the head stalls again the
  same way after that.

So a ready head with nothing running never holds the turn for more than `GRACE_S` without an
action, and the actions are bounded at two per head.

Typical usage example:

    watch = stall.on_poll(ln, watch, head, stall.of(line), waited, line)
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial

from lib.json_types import as_list
from deploy_tools import await_ci, ci_stall
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Verdict, say

from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

# GitHub registers the ready run seconds after the flip; the push-delivery grace await_ci allows is
# far wider.
GRACE_S = await_ci.DROPPED_PUSH_GRACE_S


class Stall(StrEnum):
    """Why a head's CI will not move without the landing."""

    DRAFT_SKIPPED = "draft-skipped"
    NO_RUN_LEFT = "no-run-left"


class Act(StrEnum):
    """What the merge wait does on this poll."""

    WAIT = "wait"
    ANNOUNCE = "announce"
    HOLD = "hold"
    TOGGLE = "toggle"
    RERUN = "rerun"
    GIVE_UP = "give-up"


# The actions that start a run, by the stall each one answers.
START = {Stall.DRAFT_SKIPPED: Act.TOGGLE, Stall.NO_RUN_LEFT: Act.RERUN}


@dataclass(frozen=True)
class Watch:
    """The stall the wait is sitting on, carried from one poll to the next.

    Attributes:
        head: the stalled head, or "" when the last poll read no stall.
        stall: that head's stall, or None.
        since: the `waited` its grace started at.
        tried: each (head, stall) the landing already started a run for.
    """

    head: str = ""
    stall: Stall | None = None
    since: int = 0
    tried: frozenset[tuple[str, Stall]] = frozenset()

    def retried(self) -> bool:
        """Whether the landing already started a run for the stall it sits on."""
        return self.stall is not None and (self.head, self.stall) in self.tried


def of(line: str) -> Stall | None:
    """The stall await_ci's one-poll answer names, or None."""
    if ci_stall.DRAFT_SKIPPED in line:
        return Stall.DRAFT_SKIPPED
    if ci_stall.NO_RUN_LEFT in line:
        return Stall.NO_RUN_LEFT
    return None


def step(
    watch: Watch,
    head: str,
    stall: Stall | None,
    waited: int,
    ready: Callable[[], bool],
    running: Callable[[], str],
) -> tuple[Watch, Act]:
    """The next watch and the action for one poll that read `stall` on `head`.

    Args:
        watch: what the previous poll returned; `Watch()` before the first.
        head: the head this poll read.
        stall: the stall await_ci named on it, or None.
        waited: the seconds the merge wait has run.
        ready: whether the PR is no longer a draft. Called only once the grace has run out.
        running: why a run that can still give a verdict forbids an action, or "" when none
            does. Called only once the grace has run out on a ready PR.
    """
    if stall is None:
        return replace(watch, head="", stall=None), Act.WAIT
    if (head, stall) != (watch.head, watch.stall):
        return replace(watch, head=head, stall=stall, since=waited), Act.ANNOUNCE
    if waited - watch.since < GRACE_S or not ready():
        return watch, Act.WAIT
    if running():
        return replace(watch, since=waited), Act.HOLD
    if watch.retried():
        return watch, Act.GIVE_UP
    tried = watch.tried | {(head, stall)}
    return replace(watch, since=waited, tried=tried), START[stall]


_ALREADY_STARTED = {
    Stall.DRAFT_SKIPPED: "The landing already converted it to draft and back once "
    "for this head.",
    Stall.NO_RUN_LEFT: "The landing already re-ran its cancelled run once for this "
    "head.",
}


def _is_ready(ln: Landing) -> bool:
    return ln.view("isDraft").get("isDraft") is False


def _dead_end(pr: str, watch: Watch) -> str:
    """Why the ready PR's stalled head cannot merge, with no remedy.

    `_toggle_by_hand` and `_rerun_by_hand` are the manual remedies, and a rate-limited action
    replaces them with the wait.

    A PR marked ready right after its push can get only the push's draft-era run, or a ready
    run that the draft-era run cancelled, and no other run follows the ready flip. A run
    cancelled with no skip beside it is never re-run on its own either. Either way nothing
    would ever supply a verdict.
    """
    head = watch.head[:8]
    if watch.stall is Stall.DRAFT_SKIPPED:
        why = (
            f"PR #{pr} is ready, but every CI run on {head} either ran while it was a draft "
            "and skipped every job, or was cancelled, and GitHub started no other run; a "
            "re-run of the draft run replays the draft event and skips again."
        )
    else:
        why = (
            f"PR #{pr} is ready, but every CI run on {head} finished with no verdict, and "
            "GitHub starts no other run on its own."
        )
    if watch.retried() and watch.stall is not None:
        why += " " + _ALREADY_STARTED[watch.stall]
    return why


def _toggle_by_hand(pr: str) -> str:
    return (
        f"convert it to draft and back (`gh pr ready --undo {pr}`, then "
        f"`gh pr ready {pr}`) or push, then re-run this"
    )


def _ci_runs(ln: Landing, head: str) -> list[WorkflowRun]:
    """The PR's `ci.yml` runs on `head`, newest first.

    Raises:
        gh_read.GhReadFailed, ValueError: the runs could not be read.
    """
    url = f"repos/{{owner}}/{{repo}}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request"

    def call() -> list:
        got = ln.tools.gh_json(
            "api", url, "--jq", "[.workflow_runs[] | {id, status, conclusion}]"
        )
        return as_list(got, "gh api ci.yml runs")

    return [WorkflowRun.parse(r) for r in ln.read(call, "gh api ci.yml runs")]


def ready_run_on(ln: Landing, head: str, kind: Stall = Stall.DRAFT_SKIPPED) -> str:
    """Why the PR's `ci.yml` runs on `head` forbid starting a run; "" when none can give a verdict.

    For a draft-skipped head, a run that is not completed, or that completed with a verdict,
    ran while the PR was ready. Its `prek` gate `needs` every other job, so the gate gets no
    check run until they finish, and for those minutes `await_ci` reads only the draft run's
    skipped gate. Converting to draft then cancels the ready run. A run that completed
    `skipped` allows the toggle. So does one that completed with no verdict, such as
    `cancelled`, beside a skipped run: a draft run cancels a ready run made in the same second
    (`ci_stall.draft_skipped`). A head with no skipped run is no draft case, so its cancelled
    run forbids the toggle and `merge._start_run` takes no turn for it.

    For a no-run-left head only a run that is not completed forbids the re-run, because a
    re-run in the same concurrency group cancels it.

    A failed read forbids either, because both actions can cancel a run.
    """
    try:
        runs = _ci_runs(ln, head)
    except (gh_read.GhReadFailed, ValueError) as exc:
        return f"its CI runs could not be read ({exc})"
    skipped = [r for r in runs if (r.status, r.conclusion) == ("completed", "skipped")]
    for run in runs:
        if kind is Stall.NO_RUN_LEFT:
            blocks = run.status != "completed"
        else:
            blocks = run not in skipped and not (skipped and run.no_verdict)
        if blocks:
            return f"run {run.id} is {run.conclusion or run.status}"
    return ""


def _retrigger_ready_run(ln: Landing, head: str, dead_end: str) -> str:
    """Convert the PR to draft and back so GitHub starts a ready run; "" on success.

    GitHub starts no `ci.yml` run for the ready flip of a PR whose push run skipped as a
    draft, and a re-run replays the draft event. The conversion is the only thing
    that makes one, so the landing does it once per head instead of ending `merge-timeout`
    for a person to do the same two commands. A failure returns the landing's ending, which
    leads with `dead_end`. It names the PR's state when the first command had already run,
    because a draft PR never merges. A rate-limited step's remedy is the wait, never the
    refused command.
    """
    pr = str(ln.opts.pr)
    say(
        f"PR #{pr} is ready but {head[:8]}'s CI skipped as a draft or was cancelled; converting it to draft and back"
    )
    for step, args, by_hand in (
        ("to draft", ("pr", "ready", "--undo", pr), _toggle_by_hand(pr)),
        (
            "back to ready",
            ("pr", "ready", pr),
            f"run `gh pr ready {pr}`, then re-run this",
        ),
    ):
        left = "" if step == "to draft" else f"; PR #{pr} is left as a draft"
        try:
            ln.tools.gh(*args)
        except subprocess.CalledProcessError as exc:
            why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
            return (
                f"{dead_end} Converting it {step} failed ({why}){left}; "
                + ln.remedy(why, by_hand)
            )
        except subprocess.TimeoutExpired:
            return f"{dead_end} Converting it {step} timed out{left}; {by_hand}"
    return ""


def _rerun_by_hand() -> str:
    return "re-run its newest cancelled CI run (`gh run rerun <run-id>`) or push, then re-run this"


def _rerun_cancelled(ln: Landing, head: str, dead_end: str) -> str:
    """Re-run the newest `ci.yml` run on `head` that finished with no verdict; "" on success.

    Only the newest: two re-runs in one concurrency group cancel each other. A failure returns
    the landing's ending, which leads with `dead_end`; a rate-limited one's remedy is the wait.
    """
    try:
        runs = _ci_runs(ln, head)
        run = next((r for r in runs if r.no_verdict and r.id is not None), None)
    except (gh_read.GhReadFailed, ValueError) as exc:
        return f"{dead_end} Its CI runs could not be read ({exc}); {_rerun_by_hand()}"
    if run is None:
        return f"{dead_end} No CI run on it finished cancelled; push, then re-run this"
    say(f"every CI run on {head[:8]} finished with no verdict; re-running run {run.id}")
    try:
        ln.tools.gh("run", "rerun", str(run.id))
    except subprocess.CalledProcessError as exc:
        why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
        return f"{dead_end} Re-running run {run.id} failed ({why}); " + ln.remedy(
            why, _rerun_by_hand()
        )
    except subprocess.TimeoutExpired:
        return f"{dead_end} Re-running run {run.id} timed out; {_rerun_by_hand()}"
    return ""


def on_poll(
    ln: Landing,
    watch: Watch,
    head: str,
    kind: Stall | None,
    waited: int,
    line: str,
) -> Watch:
    """Take this poll's `step` and perform its action; the watch for the next poll.

    Raises:
        Outcome: the action failed, or the head stalled again after the landing started a run.
    """
    pr = str(ln.opts.pr)
    blocker = ""

    def running() -> str:
        nonlocal blocker
        blocker = ready_run_on(ln, head, kind or Stall.DRAFT_SKIPPED)
        return blocker

    after, act = step(watch, head, kind, waited, partial(_is_ready, ln), running)
    if act is Act.ANNOUNCE:
        wait_for = (
            "a ready run"
            if kind is Stall.DRAFT_SKIPPED
            else "a run to start before re-running its cancelled one"
        )
        say(f"{line}; waiting up to {GRACE_S}s for {wait_for}")
    elif act is Act.HOLD:
        action = (
            "converting it to draft"
            if kind is Stall.DRAFT_SKIPPED
            else "re-running its cancelled run"
        )
        say(
            f"{head[:8]} reads as stalled ({kind}), but {blocker}; not {action}, which would "
            "cancel that run"
        )
    elif act in (Act.TOGGLE, Act.RERUN):
        start = _retrigger_ready_run if act is Act.TOGGLE else _rerun_cancelled
        failure = start(ln, head, _dead_end(pr, watch))
        if failure:
            ln.die(failure, 75, Verdict.MERGE_TIMEOUT)
    elif act is Act.GIVE_UP:
        by_hand = (
            _toggle_by_hand(pr) if kind is Stall.DRAFT_SKIPPED else _rerun_by_hand()
        )
        ln.die(
            f"{_dead_end(pr, after)} To get a run, {by_hand}",
            75,
            Verdict.MERGE_TIMEOUT,
        )
    return after
