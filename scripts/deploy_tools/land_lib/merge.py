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
the wait early instead, with its remedy (`stuck_checks`). --arm-merge without --await-merge
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
from functools import partial


from lib.exit_codes import CI_DISARMED, CI_GREEN, CI_PENDING, CI_RED
from lib.json_types import JsonObject, as_object
from lib.progress import OnChange
from lib import gh_rate_limit, removes_object
from deploy_tools import await_ci
from deploy_tools.land_lib import (
    closing_refs,
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
    for the turn while it waits (`line.py`). With the turn, the PR's branch is updated from
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

    in_turn, queued = False, 0
    # Read again after a wait in line, because the PR may have moved during it.
    while True:
        view = ln.view("isDraft,state,mergeable,headRefOid")
        head = view.get("headRefOid") or ""
        if view.get("state") != "OPEN" or not head:
            return "", in_turn, queued
        untested = rejoined or not stall.ready_run_on(ln, head)
        if not untested or in_turn or queued:
            break
        in_turn, queued = ask(head, view.get("isDraft") is True)
        if not queued:
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
    so the landings behind it do not wait out the same limit. Master may have moved while it
    waited, so before its merge a head master moved past gives the turn back and rejoins the
    line as a draft (`stale_base.rejoin_in_turn`).
    """
    o, t = ln.opts, ln.tools
    waited = 0
    # The `waited` no update moves the end of the wait past.
    wait_cap = 2 * o.merge_timeout
    # Whether this landing holds the landing turn, and the seconds it stood in line for it.
    in_turn, queued = False, 0
    conflicting = 0
    refused = OnChange(say)
    rate_limited = OnChange(say)
    # await_ci's last answer when it was `pending`, for the merge-timeout message; else empty.
    ci_pending = ""
    updated_from = ""
    # Whether this landing has asked for the turn, which `turn.held` answers only once.
    asked = False
    # Whether `stale_base.rejoin_line` made the PR a draft at a head GitHub has yet to move to.
    rejoined = False
    # Whether this landing made the PR a draft, so a refusal inside the turn marks it ready.
    drafted = False

    def ask(head: str, draft: bool) -> tuple[bool, int]:
        """Take the turn for `head`, reading what the turn's rules need from GitHub first."""
        nonlocal asked
        asked = True
        # A failed read stores nothing, so the rule inside the turn reads again.
        with contextlib.suppress(gh_read.GhReadFailed, ValueError):
            ln.changed_paths(head)
        try:
            return take_turn(head if draft else "")
        except Outcome:
            if drafted:
                stale_base.ready_again(ln)
            raise

    if ln.direct_merge_subject:
        updated_from, in_turn, queued = _start_run(ln, ask)
        waited = queued
    # The `waited` at which the wait ends; a wait in line or an update moves it.
    wait_ends_at = waited + o.merge_timeout
    if ln.batched_s:
        wait_ends_at = min(wait_ends_at, wait_cap)
    # The head the pin gate last passed, so a retried merge reruns the gate only for a new head.
    gated_head = ""
    # The forward-only pins that head bumps, whose land rule is read again before each retry.
    gated_pins: list[str] = []
    watch = stall.Watch()
    # The head whose checks `stuck_checks` reads, and the `waited` of its next read.
    stuck_head = ""
    stuck_read_at = 0
    while True:
        view = ln.view("state,mergeable,headRefOid")
        state = view.get("state", "")
        if state == "MERGED":
            break
        if state == "CLOSED":
            ln.die(f"PR #{o.pr} was closed without merging — nothing to land", 1)
        conflicting = conflicting + 1 if view.get("mergeable") == "CONFLICTING" else 0
        if conflicting >= 2:
            ln.die(
                f"PR #{o.pr} conflicts with {BRANCH} — rebase it, push, then re-run this",
                1,
                Verdict.MERGE_CONFLICT,
            )
        head = view.get("headRefOid") or ""
        # GitHub applies a branch update asynchronously: until the head moves, the stale head's
        # green CI is what the update discards.
        if updated_from and head == updated_from:
            head = ""
        if ln.pinned_head and head and head != ln.pinned_head:
            if updated_from != ln.pinned_head:
                ln.die(
                    f"PR #{o.pr}'s head moved from {ln.pinned_head[:8]} to {head[:8]} after "
                    "the landing policy checked it; re-run so the new head is checked",
                    1,
                )
            say(f"checking the landing policy again on the updated head {head[:8]}")
            ln.pinned_head = policy.check(ln)
            continue
        if rejoined and head:
            rejoined = False
            again, in_turn, in_line = _start_run(ln, ask, rejoined=True)
            updated_from = again or updated_from
            queued += in_line
            waited += in_line
            wait_ends_at = min(waited + o.merge_timeout, wait_cap)
            continue
        pause, spent = 0, ""
        rc, line = CI_PENDING, ""
        if head:
            try:
                rc, line = t.await_ci(head, 0)
            except await_ci.RateLimitedError as exc:
                pause, spent = exc.wait_s, exc.quota
                rate_limited(
                    f"{head[:8]}: {exc}; the merge wait backs off until it does"
                )
        if head and not pause:
            ci_pending = line if rc == CI_PENDING else ""
            watch = stall.on_poll(ln, watch, head, stall.of(ci_pending), waited, line)
            if rc == CI_RED:
                ln.die(
                    f"PR #{o.pr} cannot merge — its own CI is red ({line}); fix it, push, "
                    "and re-run this",
                    1,
                    Verdict.PR_CI_RED,
                )
            # Only this landing's direct merge waits on await_ci's green, so only it can never fire
            # on a disarmed gate.
            if rc == CI_DISARMED and ln.direct_merge_subject:
                ln.die(disarmed(f" at {head[:8]}", f"PR #{o.pr} not merged"), 1)
            if rc == CI_GREEN and ln.direct_merge_subject and not in_turn:
                if not asked and stale_base.rejoin_line(
                    ln, head, view.get("mergeable") == "CONFLICTING"
                ):
                    updated_from, rejoined, drafted = head, True, True
                    wait_ends_at = min(waited + o.merge_timeout, wait_cap)
                    continue
                in_turn, in_line = ask(head, False)
                if in_line:
                    # The PR may have moved while this landing stood in line, so read it again.
                    queued += in_line
                    waited += in_line
                    wait_ends_at = min(waited + o.merge_timeout, wait_cap)
                    # DECIDED: a ready head master moved past while it waited gives the turn
                    # back and rejoins the line as a draft (the operator, 2026-11-15, amending
                    # D10 in docs/ci-throughput.md): merged as it stands, master sweeps in full.
                    if stale_base.rejoin_in_turn(
                        ln, head, view.get("mergeable") == "CONFLICTING"
                    ):
                        in_turn = False
                        updated_from, rejoined, drafted = head, True, True
                    continue
            if rc == CI_GREEN and ln.direct_merge_subject:
                slots.refuse_before_merge(ln, head)
                if head != gated_head:
                    gated_pins = pin_gate.gate_before_merge(ln, head)
                    gated_head = head
                else:
                    pin_gate.recheck_before_merge(ln, gated_pins)
                commits = ln.view("commits").get("commits", [])
                interrupted, ln.turn_interrupted = ln.turn_interrupted, False
                if interrupted and stale_base.rejoin_in_turn(
                    ln, head, view.get("mergeable") == "CONFLICTING"
                ):
                    in_turn, updated_from, rejoined, drafted = False, head, True, True
                    continue
                if not stale_base.batch_tested(ln, head):
                    stale_base.refuse_stale_overlap(ln, head)
                refusal = _merge_directly(ln, head, commits)
                if not refusal:
                    continue
                refused(f"merge refused at {head[:8]}: {refusal}; waiting")
            if head != stuck_head:
                stuck_head, stuck_read_at = head, waited + stuck_checks.AFTER_S
            elif rc == CI_GREEN and waited >= stuck_read_at:
                stuck_read_at = waited + stuck_checks.AFTER_S
                stuck = stuck_checks.find(ln, head)
                if stuck:
                    ln.die(
                        stuck_checks.dead_end(o.pr, head, stuck),
                        75,
                        Verdict.MERGE_TIMEOUT,
                    )
        if waited >= wait_ends_at:
            # A run skipped as a draft names its own remedy in await_ci's line, because a re-run
            # skips again.
            if pause:
                why = f"GitHub refused its CI read for the rate; the quota frees in {pause}s"
            elif watch.head:
                why = f"its head has no CI verdict ({ci_pending})"
            elif ci_pending:
                why = (
                    f"its head has no CI verdict ({ci_pending}); if its run was cancelled, "
                    "none arrives on its own: re-run it with `gh run rerun <run-id>`, or push"
                )
            else:
                why = "look at its checks"
            ln.die(
                f"PR #{o.pr} still {state} after {waited}s — not being merged; {why}",
                75,
                Verdict.MERGE_TIMEOUT,
            )
        step = o.merge_poll
        if pause:
            step = ln.without_turn(
                partial(
                    gh_rate_limit.sleep_until_reset,
                    spent,
                    max(step, min(pause, wait_ends_at - waited)),
                    t.sleep,
                    ln.read_quota,
                )
            )
        else:
            t.sleep(step)
        waited += step
    ln.merge_wait_s = waited
    turn_wait = ln.ledger.turn_wait
    notes = [
        f"{turn_wait}s of it waiting for the landing turn" if turn_wait else "",
        f"{ln.batched_s}s of it on its batch's run" if ln.batched_s else "",
    ]
    say(", ".join([f"merged after {waited}s", *filter(None, notes)]))
