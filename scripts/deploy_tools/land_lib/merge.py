"""The merge phase: check the PR here, then merge it directly once its CI is green.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial.
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

--arm-merge never arms GitHub's auto-merge. Auto-merge merges whatever head is green when CI
finishes, while the direct merge here is pinned to the head await_ci read green and the landing
policy checked. Auto-merge cannot merge to master either: ruleset 27167149 lets only the
admin role and Renovate update it, and auto-merge never applies a ruleset bypass
(github/docs#45265), so an armed PR sits BLOCKED. A CHANGES_REQUESTED PR is refused outright, because someone asked for it
to change. `gh pr merge` refuses it at its own pre-flight as well (cli/cli#13388).

--await-merge polls the PR's state until merged, and merges it through the REST endpoint once
await_ci reads the head green, pinned to that head SHA. The master CI gate still refuses that
call until its own checks pass. A refusal is reported and the wait goes on, so a merge GitHub
keeps refusing reaches merge-timeout rather than a merge. A check GitHub will never finish ends
the wait early instead, with its remedy (`stuck_checks`), and so does a refusal while a required
check has failed, as `pr-ci-red` (`failing_checks`). --arm-merge without --await-merge
dies: nothing in that run would merge the PR.

A head that bumps a forward-only pin (`k3s_longhorn_version`, `k3s_metallb_version`) merges
only after `pin_gate.gate_before_merge` passes it: on daniel-box, with DEPLOY_HOLD at `none`, no
bump of that pin already in flight, its runbook gates green and `homelab/upgrade-gates` posted
on the head. `pin_gate.py`'s docstring has the order and what each step reads.

The direct merge copies every `Removes-Object:` line on the PR's commits into the squash body
it sends (`squash_body`), so `promote.py decide` finds each one on master whatever the repo's
squash default is.

--arm-merge also refuses a PR whose body carries a closing keyword outside a `Closes #N` line,
before any merge call. A "Filed and not fixed: #N" line closes #N on merge, because GitHub
reads the keyword and not the sentence around it. `closing_refs.stray_closing_refs` owns
the rule.

--arm-merge refuses while the repo is not public, before any merge call. On the free plan GitHub
enforces no ruleset on a private repo, so the direct merge would go through without the CI
gate. `_refuse_private_repo` owns it. It cannot stop an
auto-merge armed by hand while the repo was public from firing after a later flip;
github-ruleset-drift.sh is the cover for that.

`opts.require_author` (from `LAND_REQUIRE_AUTHOR`, which renovate-agent-land@.service sets to
`app/renovate`) makes --arm-merge refuse a PR by anyone else, before any merge call. The
agent's contract says "never a PR by another author", and this is the check; an
interactive session leaves the variable unset and is unaffected. The refusal names the hand-off
rather than the `--any-author` override: only the unattended session ever reads it, and the
operator chose that a `manual —` bump goes to a person.

When a lander unit sets the landing policy (`policy.py`), --arm-merge runs its checks and pins
the REST merge to the head SHA they read through its `sha=` field. --await-merge dies if the
head moves before the merge, so a push after the checks needs a re-run, which checks it again.
The one head move it does not die on is its own branch update, below: it runs the checks
again on the updated head and pins that.

--await-merge gives a PR one run on the master its merge lands on, inside the landing turn,
with one branch update at most (`stale_base.py`).
"""

import contextlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import cast


from lib.exit_codes import CI_DISARMED, CI_GREEN, CI_PENDING, CI_RED
from lib.json_types import JsonObject, as_object
from lib import gh_rate_limit, removes_object
from deploy_tools import await_ci
from deploy_tools.land_lib import (
    closing_refs,
    failing_checks,
    gh_read,
    pin_gate,
    policy,
    slots,
    stale_base,
    stall,
    stuck_checks,
)
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, disarmed, say
from deploy_tools.land_lib.pr_json import CommitMessage
from deploy_tools.land_lib.core import landing_actions as act
from deploy_tools.land_lib.core import landing_machine
from deploy_tools.land_lib.core.landing_actions import Die, End
from deploy_tools.land_lib.core.landing_state import Ci, LandingState, Setup
from deploy_tools.land_lib.transitions import CHECKS


type TakeTurn = Callable[[str], tuple[bool, int]]


def _leave_for_a_direct_merge(ln: Landing, subject: str) -> None:
    """Hand the PR to await_merge, which merges it directly once CI is green.

    Without --await-merge nothing in this run would merge it, so that dies instead.
    """
    pr = ln.opts.pr
    if not ln.opts.await_merge:
        ln.die(
            f"PR #{pr} is merged into {BRANCH} only by --await-merge — re-run with both "
            "--arm-merge --await-merge, which merges it directly once CI is green; "
            "--await-merge alone only polls",
            1,
        )
    ln.direct_merge_subject = subject
    say(f"PR #{pr}: merging directly once CI is green, at the head CI checked")


def squash_body(commits: list[CommitMessage]) -> str:
    """The squash body that carries the PR's `Removes-Object:` lines, or '' when it has none.

    `promote.py decide` refuses a promotion that loses an object no such line declares, so a
    line that misses master stalls every later promotion. Without a body of ours, GitHub writes
    the repo's squash default, which a settings change can turn into the PR description or
    nothing. The body is therefore every commit's message, in GitHub's `* <headline>` form,
    then each line again in a final paragraph, where git's trailer parser also finds it. With
    no line, '' leaves GitHub's default alone.
    """
    messages = [
        f"* {c.get('messageHeadline', '')}\n\n{c.get('messageBody', '')}".rstrip()
        for c in commits
    ]
    lines = removes_object.declared("\n\n".join(messages))
    if not lines:
        return ""
    trailers = "\n".join(f"{removes_object.KEY}: {line}" for line in sorted(lines))
    return "\n\n".join([*messages, trailers]) + "\n"


# gh's header line and HTTP status around a rule-violation refusal, which name no reason.
_REFUSAL_NOISE = re.compile(r"^gh: Repository rule violations found$|\s*\(HTTP \d+\)$")
_REQUIRED_CHECK = re.compile(r'^Required status check "([^"]+)" is (.+?)\.?$')


def refusal_reason(stderr: str) -> str:
    """gh's merge refusal as one line, e.g. `required check prek in progress`."""
    reasons = []
    for line in stderr.splitlines():
        line = _REFUSAL_NOISE.sub("", line.strip()).strip()
        if line:
            reasons.append(_REQUIRED_CHECK.sub(r"required check \1 \2", line))
    return "; ".join(reasons)


def _merge_directly(ln: Landing, head: str, commits: list[CommitMessage]) -> str:
    """Squash-merge the PR at `head` through the REST endpoint; '' on success, else why not.

    `sha` makes GitHub refuse if the head moved since await_ci read it green.
    `{owner}/{repo}` resolves from the checkout, as every `gh pr view` here does.
    When the PR's `commits` carry a `Removes-Object:` line, the merge also sends the body
    `squash_body` writes.
    """
    fields = [
        "merge_method=squash",
        f"sha={head}",
        f"commit_title={ln.direct_merge_subject}",
    ]
    body = squash_body(commits)
    if body:
        fields.append(f"commit_message={body}")
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/merge",
            *(arg for field in fields for arg in ("-f", field)),
        )
    except subprocess.CalledProcessError as exc:
        return refusal_reason(exc.stderr or "") or f"gh exited {exc.returncode}"
    except subprocess.TimeoutExpired:
        return "gh timed out"
    say(f"merged directly at the checked head: {ln.direct_merge_subject}")
    return ""


def _require_author(ln: Landing) -> None:
    """Die unless the PR's author is `opts.require_author`; a no-op when it is unset.

    Its own `gh pr view --json author` rather than a field on the state read, so a session
    with no author requirement makes no extra call. `login` is what
    `gh pr list --author` matches on too (`app/renovate` for the bot), so the unit's value
    and the wrapper's census name the author the same way.
    """
    want = ln.opts.require_author
    if not want:
        return
    have = (ln.view("author").get("author") or {}).get("login", "")
    if have != want:
        ln.die(
            f"authored by {have or '<unknown>'}, not {want} — this session may only arm "
            f"{want}'s PRs (LAND_REQUIRE_AUTHOR). Leave it open and file a hand-off finding "
            "carrying its land.sh command, so an interactive session lands it (#2746); do "
            "not pass --any-author from the session this variable is set in",
            1,
        )


def _refuse_private_repo(ln: Landing) -> None:
    """Die unless `repos/{owner}/{repo}` reads `visibility: public`.

    A failed or unparseable read dies too: an unknown visibility is an unknown merge gate,
    and the cost is one landing to re-run.
    """

    def call() -> JsonObject:
        got = ln.tools.gh_json("api", "repos/{owner}/{repo}")
        return as_object(got or {}, "gh api repos")

    try:
        repo = ln.read(call, "gh api repos")
    except gh_read.GhReadFailed as exc:
        ln.die(f"could not read the repo's visibility: {exc}", 1)
    except ValueError as exc:
        ln.die(f"could not read the repo's visibility: unparseable gh output: {exc}", 1)
    visibility = repo.get("visibility") or "<unknown>"
    if visibility != "public":
        ln.die(
            f"repo is {visibility}: rulesets are not enforced on the free plan, so the CI "
            f"merge gate is open — not merging PR #{ln.opts.pr} until the repo is public "
            "again",
            1,
        )


def arm_merge(ln: Landing) -> None:
    """Check this PR and leave it for await_merge to merge, unless it is already merged."""
    pr = ln.opts.pr
    view = ln.view("state,title,body,reviewDecision")
    if view.get("state") == "MERGED":
        say("already merged; --arm-merge is a no-op")
        return
    if view.get("state") == "CLOSED":
        ln.die(f"PR #{pr} was closed without merging — nothing to arm", 1)
    _require_author(ln)
    closing_refs.refuse_stray(ln, view.get("body") or "")
    _refuse_private_repo(ln)
    if ln.opts.policy_active:
        ln.pinned_head = policy.check(ln)
    subject = ln.opts.subject or view.get("title", "")
    review = view.get("reviewDecision")
    if review == "CHANGES_REQUESTED":
        ln.die(
            f"PR #{pr} has changes requested — address them and get a new review, then "
            "re-run this",
            1,
        )
    _leave_for_a_direct_merge(ln, subject)


def _mark_ready(ln: Landing) -> None:
    """Mark a draft PR ready: a draft's CI run runs no job, and a draft cannot merge."""
    stale_base.ready_again(ln)
    say(f"PR #{ln.opts.pr} was a draft; marked it ready")


def _no_turn(_draft: str) -> tuple[bool, int]:
    """`await_merge`'s `take_turn` for a caller that has no landing turn to offer."""
    return False, 0


def _start_run(
    ln: Landing, ask: Callable[[str, bool], tuple[bool, int]], rejoined: bool = False
) -> tuple[str, bool, int]:
    """Give a PR that has had no CI run its one run, on the master of the landing turn.

    A head with no CI run that ran jobs, or whose only such runs were cancelled beside a
    draft run (`stall.ready_run_on`), takes the landing turn first (`turn.py`), so that the
    run it is about to get tests the master its merge lands on. A draft stands in the line
    for the turn while it waits (`line.py`), and again through a step out of the turn, so the
    PR is read again after either: a PR the holder merged from the line is left as it is.
    With the turn, the PR's branch is updated from
    master when master is ahead of it, and only then is a draft marked ready, because a
    draft's run runs no job and a draft cannot merge. GitHub builds the tree a pull-request
    run tests when the PR's branch is pushed. A ready flip does not build it again, so a run
    the flip starts tests the master of the push. The update is a push, so the run the flip after it starts tests the master of the
    turn. A flip before the update started a run on the stale head, which the update's push
    cancels after it has held runners for minutes.

    Any other head with a CI run that ran jobs keeps it and takes no turn here:
    `await_merge` takes
    the turn once that head is green, so no landing holds the turn while a run that started
    before it finishes. The branch is also left as it is for a head that moved after the
    landing policy pinned it, a head admitted on an approval, a PR GitHub reads as
    conflicting, and an update that fails. A head a landing batch tested is left too
    (`batch.py`): the batch stacked it on the master of the turn, and an update would make a
    head no run tested.

    Returns:
        The head the branch was updated from, or "" when it was not updated; whether the
        landing holds the turn; and the seconds it waited for it.
    """

    in_turn, queued, asked = False, 0, False
    # Read again after a wait in line, because the PR may have moved during it. A step out of
    # the turn waits in line too, and the holder may merge the PR then.
    while True:
        view = ln.view("isDraft,state,mergeable,headRefOid")
        head = view.get("headRefOid") or ""
        if view.get("state") != "OPEN" or not head:
            return "", in_turn, queued
        untested = rejoined or not stall.ready_run_on(ln, head)
        if not untested or asked:
            break
        in_turn, queued = ask(head, view.get("isDraft") is True)
        asked = True
        if not (queued or ln.turn_interrupted):
            break
    draft = view.get("isDraft") is True
    if (
        not (in_turn and untested)
        or stale_base.batch_tested(ln, head)
        or view.get("mergeable") == "CONFLICTING"
        or head == ln.approved_head
        or (ln.pinned_head and head != ln.pinned_head)
    ):
        if draft:
            _mark_ready(ln)
        return "", in_turn, queued
    updated = stale_base.update_for_run(ln, head, draft)
    # Marked ready only after the update, so the one run starts on the updated head, not one the
    # update's push cancels.
    if draft:
        _mark_ready(ln)
    return (head if updated else ""), in_turn, queued


def take(
    ln: Landing, take_turn: TakeTurn, head: str, draft: bool, drafted: bool
) -> tuple[bool, int]:
    """Take the turn for `head`, reading what the turn's rules need from GitHub first.

    Returns:
        What `take_turn` returns: whether this landing holds the turn, and the seconds it
        stood in line.

    Raises:
        Outcome: the wait for the turn ended the landing. A PR this landing made a draft
            (`drafted`) is marked ready first.
    """
    # A failed read stores nothing, so the rule inside the turn reads again.
    with contextlib.suppress(gh_read.GhReadFailed, ValueError):
        ln.changed_paths(head)
    try:
        return take_turn(head if draft else "")
    except Outcome:
        if drafted:
            stale_base.ready_again(ln)
        raise


def ending(pr: str, die: Die) -> tuple[str, int, Verdict | None]:
    """The message, exit code and verdict of a merge wait that ends without a merge."""
    head = die.head[:8]
    match die.end:
        case End.CLOSED:
            return f"PR #{pr} was closed without merging — nothing to land", 1, None
        case End.CONFLICT:
            return (
                f"PR #{pr} conflicts with {BRANCH} — rebase it, push, then re-run this",
                1,
                Verdict.MERGE_CONFLICT,
            )
        case End.HEAD_MOVED:
            return (
                f"PR #{pr}'s head moved from {die.detail[:8]} to {head} after "
                "the landing policy checked it; re-run so the new head is checked",
                1,
                None,
            )
        case End.CI_RED:
            return (
                f"PR #{pr} cannot merge — its own CI is red ({die.detail}); fix it, push, "
                "and re-run this",
                1,
                Verdict.PR_CI_RED,
            )
        case End.DISARMED:
            return disarmed(f" at {head}", f"PR #{pr} not merged"), 1, None
        case End.FAILING_CHECK:
            return (
                f"PR #{pr} cannot merge — required check {', '.join(die.names)} "
                f"failed at {head}; fix it, push, and re-run this",
                1,
                Verdict.PR_CI_RED,
            )
        case End.STUCK_CHECK:
            return (
                stuck_checks.dead_end(pr, die.head, list(die.names)),
                75,
                Verdict.MERGE_TIMEOUT,
            )
        case End.TIMEOUT_RATE_LIMITED:
            why = f"GitHub refused its CI read for the rate; the quota frees in {die.detail}s"
        case End.TIMEOUT_STALLED:
            why = f"its head has no CI verdict ({die.detail})"
        case End.TIMEOUT_PENDING:
            why = (
                f"its head has no CI verdict ({die.detail}); if its run was cancelled, "
                "none arrives on its own: re-run it with `gh run rerun <run-id>`, or push"
            )
        case End.TIMEOUT:
            why = "look at its checks"
    return (
        f"PR #{pr} still {die.pr_state} after {die.waited}s — not being merged; {why}",
        75,
        Verdict.MERGE_TIMEOUT,
    )


def merged(ln: Landing, waited: int) -> None:
    """Record and report a merge wait that merged the PR after `waited` seconds."""
    ln.merge_wait_s = waited
    turn_wait = ln.ledger.turn_wait
    notes = [
        f"{turn_wait}s of it waiting for the landing turn" if turn_wait else "",
        f"{ln.batched_s}s of it on its batch's run" if ln.batched_s else "",
    ]
    say(", ".join([f"merged after {waited}s", *filter(None, notes)]))


def await_merge(ln: Landing, take_turn: TakeTurn = _no_turn) -> None:
    """Poll until merged, merging directly once CI is green. Bail early on two dead ends.

    Only CONFLICTING may bail, and only on two consecutive polls: GitHub computes
    mergeability asynchronously and serves UNKNOWN until it settles, and master moving under
    the PR flips the field for one poll. A red PR CI is the other dead end; GitHub says only
    `BLOCKED`, which it also says while checks run and for every green PR into master under
    the update restriction, so await_ci owns that verdict,
    one-shot. Only its exit 1 bails: `pending` IS the grace period, derived rather than
    guessed.

    A PR arm_merge left for a direct merge is merged here, on the first poll where await_ci
    reads its head green. A green ready head master moved past rejoins the line as a draft,
    before it asks for the turn or, when master moved while it waited, by giving the turn
    back (`stale_base.rejoin_line`). A stale head that reaches the merge anyway merges as it
    stands unless master changed a path the PR changes (`stale_base.refuse_stale_overlap`).

    `take_turn` takes the landing turn (`turn.held`): it waits in line and returns whether
    this landing holds the turn and the seconds that call waited. A head with no CI run takes
    it before its one run (`_start_run`), and any other head on the first poll that reads it
    green, so a landing never holds the turn while a run that started before it finishes.
    The seconds in line count as
    waited, and the merge wait gets one `merge_timeout` after them. No update moves the end
    of the wait past twice `merge_timeout`, so the turn wait, the merge wait and an update
    together stay inside the two merge budgets LAND_DEADLINE counts.
    Without --arm-merge this only polls, for a PR merged some other way.

    Before each ask for the turn, the PR's file list is read for the head about to ask
    (`Landing.changed_paths`), so a rate-limit wait on that read holds no turn. The rules the
    turn runs reuse the read while the head stays the same.

    A green head that has not asked for the turn, and whose run tested a master that has
    moved since, goes back in the line instead of asking (`stale_base.rejoin_line`). Once
    GitHub has moved the head, `_start_run` stands it in the line as a draft, and that update
    is the one this landing makes.

    A head a landing batch tested (`batch.py`) merges with no stale-base check while it is
    the head the batch stacked and master is still the commit the batch stacked it on
    (`stale_base.batch_tested`). The seconds of the batch's run reach this wait with the
    seconds in line for the turn, and the wait then ends inside the two merge budgets.

    A head whose CI no longer moves on its own is named on the poll that first reads it:
    every run skipped as a draft or was cancelled (`ci_stall.draft_skipped`), or every
    check-run finished with no verdict (`ci_stall.no_run_left`). Once it has stayed so for
    `stall.GRACE_S`, a ready PR gets one run for that head and stall: a draft toggle
    (`stall.on_poll`) or a re-run of its newest cancelled run. The
    grace then starts again, and a head that stalls the same way after it ends the wait.
    Neither happens while a `ci.yml` run that can still give a verdict exists on the head
    (`stall.ready_run_on`); the grace restarts. `stall.step` owns the decision.

    A head the wait has sat on for `stuck_checks.AFTER_S` has its checks read on a poll where
    await_ci reads it green, and again each `AFTER_S` after. A check left pending under a completed workflow run
    ends the wait `merge-timeout` with the empty-commit remedy, because no poll ever clears it.

    A CI read GitHub refuses for its rate sleeps until the quota frees rather than one merge
    poll, counted as waited and cut short at `merge_timeout`. The sleep re-reads the quota and
    ends at its reset (`gh_rate_limit.sleep_until_reset`).

    A landing that holds the turn gives it back for the length of any rate-limit wait, its CI
    read's or a gh read's, and asks for it again before it goes on (`Landing.without_turn`),
    so the landings behind it do not wait out the same limit. That ask can end without the
    turn. Every answer reports whether the landing still holds it (`Landing.holds_turn`), so
    the wait then goes on as a landing with no turn: a pending head enters neither `testing`
    nor `proving`, and a green head asks for the turn before its merge. Master may have moved
    while it waited, so before its merge a head master moved past gives the turn back and
    rejoins the line as a draft (`stale_base.rejoin_in_turn`).

    Just before the merge call, a landing whose turn was held over an ssh session that has
    since ended (`turn.SshLock.lost`) does not merge: another landing may hold the turn by
    then. It gives the turn back and asks for it again, as after a rate-limit wait, and polls
    on. A batch member reads its holder's turn, so a lost one ends the batch there.

    `core/landing_machine.step` decides every call above from one `LandingState`, and
    `_perform` makes each call it returns.
    """
    o = ln.opts
    setup = Setup(
        pr=o.pr,
        merge_timeout=o.merge_timeout,
        merge_poll=o.merge_poll,
        direct=bool(ln.direct_merge_subject),
        stuck_after_s=stuck_checks.AFTER_S,
    )
    first = LandingState(
        setup=setup,
        pinned_head=ln.pinned_head,
        tested_head=ln.tested_head,
        batched=bool(ln.batched_s),
    )
    _drive(ln, take_turn, first)


def _drive(ln: Landing, take_turn: TakeTurn, s: LandingState) -> None:
    """Run the merge wait from `s` to its end, making each call `step` returns.

    Raises:
        Outcome: the wait ended without a merge, or a call ended the landing.
    """
    s, todo = landing_machine.start(s)
    while True:
        *reports, call = todo
        for report in reports:
            _perform(ln, take_turn, report)
        answer = _perform(ln, take_turn, call)
        if isinstance(call, act.ENDS):
            return
        s, todo = landing_machine.step(s, answer)


_CI = {
    CI_GREEN: Ci.GREEN,
    CI_RED: Ci.RED,
    CI_PENDING: Ci.PENDING,
    CI_DISARMED: Ci.DISARMED,
}


def _perform(ln: Landing, take_turn: TakeTurn, action: act.Action) -> act.Observation:
    """Carry out one action through the module that owns it.

    Returns:
        The answer `step` takes next, with whether the landing holds the turn once the action
        returned. An action that only reports answers `Done`.
    """
    return replace(_call(ln, take_turn, action), holds_turn=ln.holds_turn())


def _call(ln: Landing, take_turn: TakeTurn, action: act.Action) -> act.Observation:
    """`_perform`'s call for `action`, and its answer before the turn is read."""
    t = ln.tools
    match action:
        case act.StartRun(rejoined=rejoined, drafted=drafted):
            asked = False

            def ask(head: str, draft: bool) -> tuple[bool, int]:
                nonlocal asked
                asked = True
                return take(ln, take_turn, head, draft, drafted)

            updated, in_turn, in_line = _start_run(ln, ask, rejoined=rejoined)
            return act.RunStarted(
                updated, in_turn, in_line, asked, ln.tested_head, bool(ln.batched_s)
            )
        case act.ReadView():
            view = ln.view("state,mergeable,headRefOid")
            return act.Viewed(
                view.get("state", ""),
                view.get("mergeable") == "CONFLICTING",
                view.get("headRefOid") or "",
            )
        case act.CheckPolicy():
            ln.pinned_head = policy.check(ln)
            return act.PolicyChecked(ln.pinned_head)
        case act.ReadCi(head=head):
            checks: list[tuple[int, int]] = []
            try:
                rc, line = t.await_ci(head, 0, lambda d, n: checks.append((d, n)))
            except await_ci.RateLimitedError as exc:
                return act.CiRateLimited(exc.wait_s, exc.quota, str(exc))
            return act.CiRead(
                _CI.get(rc, Ci.OTHER),
                line,
                stall.of(line),
                checks[-1] if checks else None,
            )
        case act.WatchStall(
            watch=watch, head=head, stall=kind, waited=waited, line=line
        ):
            return act.StallWatched(stall.on_poll(ln, watch, head, kind, waited, line))
        case act.RejoinLine(head=head, conflicting=conflicting):
            return act.Answer(stale_base.rejoin_line(ln, head, conflicting))
        case act.AskTurn(head=head, drafted=drafted):
            in_turn, in_line = take(ln, take_turn, head, False, drafted)
            return act.TurnAnswered(in_turn, in_line, ln.tested_head)
        case act.RejoinInTurn(head=head, conflicting=conflicting):
            return act.Answer(stale_base.rejoin_in_turn(ln, head, conflicting))
        case act.RefuseBeforeMerge(head=head):
            slots.refuse_before_merge(ln, head)
        case act.GatePins(head=head):
            return act.PinsGated(tuple(pin_gate.gate_before_merge(ln, head)))
        case act.RecheckPins(pins=pins):
            pin_gate.recheck_before_merge(ln, list(pins))
        case act.ReadCommits():
            return act.CommitsRead(tuple(ln.view("commits").get("commits", [])))
        case act.ReadTested(head=head):
            interrupted, ln.turn_interrupted = ln.turn_interrupted, False
            return act.Tested(stale_base.batch_tested(ln, head), interrupted)
        case act.RefuseStaleOverlap(head=head):
            stale_base.refuse_stale_overlap(ln, head)
        case act.ReadTurnLost():
            return act.Answer(ln.turn_lost())
        case act.StepOut(reason=reason):
            ln.without_turn(lambda: None, reason)
        case act.Merge(head=head, commits=commits):
            return act.MergeAnswered(
                _merge_directly(ln, head, cast(list[CommitMessage], list(commits)))
            )
        case act.FindFailing(head=head, refusal=refusal):
            return act.Names(tuple(failing_checks.find(ln, head, refusal)))
        case act.FindStuck(head=head):
            return act.Names(tuple(stuck_checks.find(ln, head)))
        case act.Sleep(seconds=seconds):
            t.sleep(seconds)
            return act.Slept(seconds)
        case act.WaitOutRateLimit(quota=quota, seconds=seconds):
            sleep = partial(
                gh_rate_limit.sleep_until_reset, quota, seconds, t.sleep, ln.read_quota
            )
            return act.Slept(ln.without_turn(sleep))
        case act.Say(text=text):
            say(text)
        case act.Enter(state=state):
            ln.transitions.enter(state)
        case act.Progress(done=done, total=total):
            ln.transitions.progress(done, total, CHECKS)
        case act.Die():
            ln.die(*ending(ln.opts.pr, action))
        case act.Merged(waited=waited):
            merged(ln, waited)
    return act.Done()
