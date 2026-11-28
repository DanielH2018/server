"""A landing batch: the drafts in the line, tested with the holder's own in one CI run.

A landing that takes the turn with a draft that has had no CI run would start that draft's
one run and hold the turn until it ends, while every landing behind it waits. `form` gives
the drafts in the line (`line.py`) that same run. It stacks their heads on the holder's own
on a `queue/<PR>` branch, opens the batch's pull request and waits for its run. That run's
gate records, on each head, the tree its squash merge makes (`scripts/dev/tested_tree.py`).

`land` then merges the holder's PR and each member's in the order they were stacked. Each
carries the `landing-batch` label when it is marked ready, so its own run asks whether the
head has that record and skips every other job. Each squash merge makes the tree the batch
tested, so master's run for it skips too (`docs/ci-throughput.md`, slice B3).

The stack is built on GitHub, one `merges` call a head, so the landing pushes nothing and
the same code serves both hosts. A merge commit there has two parents and a squash merge has
one, and both make the same tree.

What a member is. A draft from the line, written there by a landing of this landing's own
user, that this landing would have been allowed to land: `merge.arm_merge` runs on it with
this landing's options, the landing policy included. Its own landing ran the same checks
with its own options before it stood in the line. A fork's branch and a serial PR
(`live_limit.kind`) are left in the line. A member merges
under its PR's title, whatever `--subject` its own landing was given.

What the label must never do. A labelled run on a head that carries a record skips every
job, and it compares no tree. So a PR is labelled only by the landing that is about to merge
it, only at the head the batch stacked, and only while master is the commit that head was
stacked on (`stale_base.batch_tested`). A PR that was labelled and marked ready and then did
not merge gets a full run before anything else lands it (`_full_run`).

`LAND_BATCH=0` turns batches off for one landing. Such a landing forms none, and it can
still be a member of another landing's batch.

A batch never makes a landing fail. A batch that cannot be formed and a member that cannot
merge each leave the PRs concerned to land alone, as they would have without the batch. The
holder's own landing goes on with its own PR either way.

A batch whose run is not green splits in two: the holder's PR and its members. The holder
forms no second batch, so its next run tests its PR alone, and that run says which side
holds the red PR (`_settle`). Until it does, each member's place reads `split`, so no
other holder stacks it. When the run is red, the red PR was the holder's own, and the
members go back to the line for the next holder to stack. Otherwise the red PR is among the
members, or the run's verdict was never read, and each member lands alone. The split costs
no run the members landing alone would not: the holder's own run happens either way. A
member goes back to the line only behind a holder whose own run is red, so a red member is
stacked again only while every holder it is stacked behind is red too.

A member's gh read that GitHub refuses for its rate waits out the limit with the holder's turn
given back (`Landing.without_turn`), so no landing behind the holder waits through it. Master
may move during that wait, so the batch ends with it. While the batch forms, the holder forms
none and lands its PR alone. While it merges, that member and the ones after it land alone.
The holder's own rate-limit wait keeps the batch, because the holder reads master again after
it, unless the holder could not take the turn back. The batch then ends the same way.
A member reads the holder's turn just before its merge (`Landing.turn_lost`). When the ssh
session that held it has ended, the member gives the turn back the same way, and it and the
members after it land alone.

A holder whose landing code is older than master's forms its batch from master's code: it hands
the turn to a landing of its PR from a fresh checkout (`turn.HandOver`), which forms the batch.
When the turn cannot be handed over, the holder lands its PR alone.

The time a batch takes. Its run counts in the holder's merge wait, which still ends inside
two merge budgets. The members' merges come out of what that wait left of those two budgets,
up to `MEMBER_TIMEOUT_S` each, so a batch adds nothing to what `LAND_DEADLINE` counts. A
member that would not fit lands alone.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial


from deploy_tools import await_ci
from deploy_tools.land_lib import (
    classify,
    gh_read,
    line,
    live_limit,
    merge,
    slots,
    stale_base,
    turn,
)
from deploy_tools.land_lib.core import queue_machine, queue_state
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say
from deploy_tools.land_lib.core.landing_state import State
from deploy_tools.land_lib.transitions import CHECKS, Transitions
from lib.exit_codes import CI_GREEN, CI_PENDING, CI_RED
from lib.json_types import JsonValue, as_object

# The label a member carries while its own run stands in for the batch's (`ci.yml`'s `prove`).
LABEL = "landing-batch"
# How many PRs one batch holds, the holder's own included; the batch's gate reads no more.
# DECIDED: 6. The operator raised it from 4 on 2026-11-27 (docs/landing-speed.md): every batch
# in that day's burst was full while 10 or more PRs stood in line. A larger batch is red more
# often, and a red batch splits; lower it from the red-batch rate.
MAX_STACKED = 6
# The seconds the batch's one full run may take. A run can take 993 s under a saturated pool.
RUN_TIMEOUT_S = 2700
MEMBER_TIMEOUT_S = 600

# What GitHub failing to answer looks like, for a call the batch makes.
_GH_FAILED = (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError)


@dataclass
class Member:
    """A draft from the line that the batch tests after the holder's own PR.

    Attributes:
        ln: A landing of the member's PR, armed with the holder's options.
        head: The head its own landing stood in the line with.
    """

    ln: Landing
    head: str


_CONFLICTS_WITH_MASTER = f"it conflicts with {BRANCH}"

# Why a holder stops its batch once its own rate-limit wait could not take the turn back.
_TURN_LOST = "this landing could not take the landing turn back after a rate-limit wait"


class _TurnGivenBack(Outcome):
    """A member's rate-limit wait gave the holder's landing turn back, which ends the batch."""


def _wait_without_turn[T](
    holder: Landing, member: Landing, wait: Callable[[], T], reason: str
) -> T:
    """`wait()` for the PR `member` lands, with the turn `holder` holds given back meanwhile.

    Raises:
        _TurnGivenBack: always, once the wait returns.
    """
    holder.without_turn(wait, reason)
    pr = member.opts.pr
    why = f"PR #{pr}'s merge gave the landing turn back {reason}"
    raise _TurnGivenBack(1, f"PR #{pr} — {why}", error=why)


def members_line(numbers: list[str]) -> str:
    """The line of the batch PR's body that names its PRs, as the batch's gate reads it."""
    return "Members: " + " ".join(f"#{number}" for number in numbers)


def _api(ln: Landing, method: str, path: str, *fields: str, jq: str = "") -> JsonValue:
    """One call to this repository's REST API; the decoded answer, or None for no body."""
    args = ["api", "-X", method, f"repos/{{owner}}/{{repo}}/{path}"]
    for field in fields:
        args += ["-f", field]
    return ln.tools.gh_json(*args, *(["--jq", jq] if jq else []))


def _alone(ln: Landing, head: str) -> str:
    """Why `ln`'s PR must land alone, by the files it changes at `head`; "" when it may stack."""
    try:
        paths = ln.changed_paths(head)
    except (gh_read.GhReadFailed, ValueError) as exc:
        return f"its files could not be read ({exc})"
    if live_limit.kind(paths) == live_limit.SERIAL:
        return f"it changes {live_limit.SERIAL_WHAT}"
    return ""


def _unfit(member: Landing, head: str) -> str:
    """Why the PR `member` lands cannot be stacked at `head`; "" when it can."""
    view = member.view("isDraft,state,mergeable,headRefOid")
    if view.get("state") != "OPEN" or view.get("isDraft") is not True:
        return "it is no longer an open draft"
    if view.get("headRefOid") != head:
        return "its head moved after its landing stood in the line"
    if view.get("mergeable") == "CONFLICTING":
        return _CONFLICTS_WITH_MASTER
    where = member.view("baseRefName,isCrossRepository")
    if (
        where.get("isCrossRepository") is not False
        or where.get("baseRefName") != BRANCH
    ):
        return f"it is not a branch of this repository into {BRANCH}"
    return ""


def _member(ln: Landing, pr: str) -> Landing:
    """A landing of the draft `pr` with `ln`'s options, to check and merge it in `ln`'s batch."""
    opts = replace(ln.opts, pr=pr, subject="", merge_timeout=MEMBER_TIMEOUT_S)
    member = Landing(opts, ln.tools, ln.classifier)
    member.transitions = Transitions(pr, None)
    member.step_out_of_turn = partial(_wait_without_turn, ln, member)
    member.turn_lost = ln.turn_lost
    return member


def _why_not(check: Callable[[], str]) -> str:
    """What `check` returns, or the error of the `Outcome` it raises.

    Raises:
        _TurnGivenBack: from `check`, which ends the batch.
    """
    try:
        return check()
    except _TurnGivenBack:
        raise
    except Outcome as exc:
        return exc.error


def _left_out(ln: Landing, member: Member, why: str) -> bool:
    """Whether `why` leaves `member` out of `ln`'s batch; it says so, and answers a conflict."""
    if not why:
        return False
    say(f"leaving PR #{member.ln.opts.pr} out of the batch: {why}")
    if why == _CONFLICTS_WITH_MASTER:
        _answer(ln, member, line.CONFLICT)
    return True


def _bars(member: Landing, head: str) -> str:
    """Why `member`'s draft may not be stacked at `head`; "" when it may.

    `_members` has already found that it need not land alone (`_alone`).
    """
    why = _unfit(member, head) or slots.bar_member(member, head)
    if not why:
        merge.arm_merge(member)
    if not why and member.pinned_head not in ("", head):
        why = "the landing policy checked another head"
    return why


def _members(ln: Landing, head: str) -> list[Member]:
    """The drafts in the line this landing may stack behind its own `head`, oldest first.

    `queue_machine.stackable` decides the order and skips a PR that must land alone. The
    other checks run here, one draft at a time, until the batch holds `MAX_STACKED` PRs.
    """
    try:
        waiting = turn.waiting(ln)
    except OSError as exc:
        say(f"could not read the line for the landing turn ({exc}); no batch")
        return []
    if not waiting:
        say(f"no batch: no other draft stands in the line behind PR #{ln.opts.pr}")
        return []
    # `_merge_members` merges a member only inside this landing's two merge budgets.
    if ln.opts.merge_timeout < MEMBER_TIMEOUT_S:
        say(
            f"no batch: merge_timeout={ln.opts.merge_timeout}s is under the "
            f"{MEMBER_TIMEOUT_S}s one member's merge may take"
        )
        return []
    # DECIDED: the landing's code is compared with master here, once, and not again before a
    # member's merge. Until the last member merges, master moves only by this batch's own
    # merges, so a member merges under the code and the policy that admitted it.
    why = _alone(ln, head)
    if not why:
        why = classify.code_lags_master(ln)
        if why and ln.hand_over_turn:
            ln.hand_over_turn(
                f"this landing holds the landing turn, and {why}; a landing of PR "
                f"#{ln.opts.pr} from a checkout of origin/{BRANCH} takes the turn over to "
                "test the drafts in the line"
            )
    if why:
        say(f"no batch: PR #{ln.opts.pr} lands alone, because {why}")
        return []
    checked: dict[str, Member] = {}

    def lands_alone(place: queue_state.Place) -> bool:
        member = checked[place.pr] = Member(_member(ln, place.pr), place.head)
        say(
            f"checking PR #{place.pr} from the line, as this landing checked its own PR"
        )
        return _left_out(ln, member, _why_not(partial(_alone, member.ln, place.head)))

    members: list[Member] = []
    for place in queue_machine.stackable(
        queue_state.Queue(tuple(waiting), lands_alone)
    ):
        member = checked[place.pr]
        if _left_out(ln, member, _why_not(partial(_bars, member.ln, member.head))):
            continue
        # The merge wait ends, and merges nothing, if the head moves from the one stacked.
        member.ln.pinned_head = member.ln.tested_head = member.head
        members.append(member)
        # Before the next place, so `stackable` reads no PR's files past the cap.
        if len(members) == MAX_STACKED - 1:
            break
    return members


def _merge_into(ln: Landing, branch: str, head: str, subject: str) -> str:
    """Merge `head` into `branch` on GitHub; the new tip, or "" when it does not merge."""
    try:
        merged = _api(
            ln,
            "POST",
            "merges",
            f"base={branch}",
            f"head={head}",
            f"commit_message={subject}",
            jq="{sha}",
        )
    except subprocess.CalledProcessError as exc:
        if "HTTP 409" in (exc.stderr or ""):
            return ""
        raise
    # GitHub answers a head the branch already holds with no body.
    return str(as_object(merged, "a merge").get("sha") or "") if merged else ""


def _drop_branch(ln: Landing, branch: str, may_be_gone: bool = False) -> None:
    """Delete `branch`, which closes its pull request.

    A failure is said, with the branch's name, unless `may_be_gone` allows for a branch that
    was never there.
    """
    try:
        _api(ln, "DELETE", f"git/refs/heads/{branch}")
    except _GH_FAILED as exc:
        if not may_be_gone:
            say(
                f"could not delete the batch's branch {branch} ({exc}); its pull request "
                "stays open until the branch is deleted"
            )


def _master_tip(ln: Landing) -> str:
    ref = ln.read(
        lambda: _api(ln, "GET", f"git/ref/heads/{BRANCH}", jq="{sha: .object.sha}"),
        "gh api git/ref",
    )
    return str(as_object(ref, BRANCH).get("sha") or "")


def _stack(
    ln: Landing, branch: str, base: str, head: str, members: list[Member]
) -> str:
    """Build `branch`: the master commit `base`, then `head`, then each member that merges.

    A member that conflicts with the PRs before it is removed from `members` and said.

    Returns:
        The branch's tip, or "" when the holder's own head does not merge.
    """
    # An earlier landing of this PR may have left its branch behind.
    _drop_branch(ln, branch, may_be_gone=True)
    _api(ln, "POST", "git/refs", f"ref=refs/heads/{branch}", f"sha={base}")
    tip = _merge_into(ln, branch, head, ln.direct_merge_subject)
    if not tip:
        return ""
    for member in list(members):
        merged = _merge_into(ln, branch, member.head, member.ln.direct_merge_subject)
        if merged:
            tip = merged
            continue
        members.remove(member)
        if _merges_onto(ln, f"{branch}-{member.ln.opts.pr}", base, member):
            say(
                f"leaving PR #{member.ln.opts.pr} out of the batch: it does not merge "
                "onto the PRs stacked before it"
            )
        else:
            say(
                f"leaving PR #{member.ln.opts.pr} out of the batch: {_CONFLICTS_WITH_MASTER}"
            )
            _answer(ln, member, line.CONFLICT)
    return tip


def _merges_onto(ln: Landing, branch: str, base: str, member: Member) -> bool:
    """Whether `member`'s head merges onto the master commit `base` by itself.

    Builds `branch` for the one merge and deletes it. A failed call reads as merging, so the
    member waits on as it did before rather than ending on a guess.
    """
    try:
        _api(ln, "POST", "git/refs", f"ref=refs/heads/{branch}", f"sha={base}")
        try:
            _api(
                ln,
                "POST",
                "merges",
                f"base={branch}",
                f"head={member.head}",
                "commit_message=conflict check",
            )
        finally:
            _drop_branch(ln, branch, may_be_gone=True)
    except subprocess.CalledProcessError as exc:
        return "HTTP 409" not in (exc.stderr or "")
    except _GH_FAILED:
        return True
    return True


def _await_run(ln: Landing, tip: str) -> tuple[int, str]:
    """Wait for the batch's run on `tip`.

    Returns:
        The seconds waited, and why the run is no green one, or "" when it is green.
    """
    waited = 0
    while True:
        try:
            rc, said = ln.tools.await_ci(tip, 0, ln.transitions.counter(CHECKS))
        except await_ci.RateLimitedError as exc:
            rc, said = CI_PENDING, str(exc)
        if rc == CI_GREEN:
            return waited, ""
        if rc == CI_RED:
            return waited, f"its run is red ({said})"
        if rc != CI_PENDING:
            return waited, said
        if waited >= RUN_TIMEOUT_S:
            return waited, f"its run gave no verdict in {waited}s ({said})"
        ln.tools.sleep(ln.opts.merge_poll)
        waited += ln.opts.merge_poll


def _label(ln: Landing, pr: str) -> None:
    _api(ln, "POST", f"issues/{pr}/labels", f"labels[]={LABEL}")


def _full_run(ln: Landing, pr: str) -> None:
    """Take the label off `pr`, and give it a full CI run if it was marked ready.

    A PR that was marked ready under the label has a run that skipped every job. Converting
    it to draft and back starts a run with no label, which runs them all, so no later
    landing merges the PR on the skipped one.
    """
    try:
        _api(ln, "DELETE", f"issues/{pr}/labels/{LABEL}")
        now = _api(ln, "GET", f"pulls/{pr}", jq="{state, draft}")
        seen = as_object(now, "a pull request")
        if seen.get("state") != "open" or seen.get("draft") is not False:
            return
        ln.tools.gh("pr", "ready", "--undo", pr)
        ln.tools.gh("pr", "ready", pr)
        say(
            f"PR #{pr} gets a full run: converted it to draft and back without the label"
        )
    except _GH_FAILED as exc:
        say(
            f"could not give PR #{pr} a full run ({exc}); remove its {LABEL} label and "
            f"convert it to draft and back before it lands"
        )


def form(ln: Landing, head: str) -> int:
    """Test the drafts in the line with this landing's own draft, in one CI run.

    The caller holds the landing turn, and `head` is the head of its draft, which has had no
    CI run. On a green run the landing's `tested_on` and `tested_head` name the master commit
    and the head the batch stacked, its `batch` holds the members, and its own PR carries the
    label for the run its ready flip starts.

    Returns:
        The seconds the batch's run took; 0 when no run was started.
    """
    pr = ln.opts.pr
    ln.ledger.batch = 1
    try:
        members = _members(ln, head)
    except _TurnGivenBack as exc:
        say(f"no batch: {exc.error}; PR #{pr} lands alone")
        return 0
    if not members:
        return 0
    branch = f"queue/{pr}"
    waited = 0
    try:
        try:
            base = _master_tip(ln)
            if ln.give_back_turn is None:
                say(f"no batch: {_TURN_LOST}; PR #{pr} lands alone")
                return 0
            tip = _stack(ln, branch, base, head, members)
            if not tip:
                say(f"no batch: PR #{pr}'s own head does not merge onto {BRANCH}")
            if not tip or not members:
                return 0
            numbers = [pr, *(member.ln.opts.pr for member in members)]
            named = " ".join(f"#{number}" for number in numbers)
            body = (
                f"The landing of #{pr} holds the landing turn and tests these pull "
                "requests in one CI run, stacked in this order. Each one is then "
                "squash-merged by itself, and this pull request is closed unmerged.\n\n"
                f"{members_line(numbers)}\n"
            )
            _api(
                ln,
                "POST",
                "pulls",
                f"title=Landing batch: {named}",
                f"head={branch}",
                f"base={BRANCH}",
                f"body={body}",
                jq="{number}",
            )
            say(f"testing {named} in one run, stacked on {base[:8]} as {branch}")
            ln.ledger.batch = len(numbers)
            ln.transitions.enter(State.TESTING)
            waited, why = _await_run(ln, tip)
            if why:
                say(
                    f"the batch is not green: {why}; PR #{pr}'s own run says whether "
                    "the red PR is its own or one of the others"
                )
                ln.transitions.enter(State.SPLIT)
                ln.lands_alone, ln.split_members = True, members
                for member in members:
                    _answer(ln, member, line.SPLIT)
                return waited
            _label(ln, pr)
        finally:
            _drop_branch(ln, branch, may_be_gone=True)
    except (*_GH_FAILED, gh_read.GhReadFailed) as exc:
        say(f"could not form a landing batch ({exc}); PR #{pr} lands alone")
        return waited
    ln.tested_on, ln.tested_head, ln.batch = base, head, members
    say(f"the batch is green after {waited}s; merging {named} in that order")
    return waited


def _answer(ln: Landing, member: Member, what: str) -> None:
    """Write `what` into the member's place in the line, for its own landing to read."""
    pr = member.ln.opts.pr
    try:
        turn.line_of(ln)("answer", pr, member.head, what)
    except OSError as exc:
        say(f"could not write {what or 'no answer'} into PR #{pr}'s place ({exc})")


def _settle(ln: Landing, own_red: bool) -> None:
    """Answer the members of this landing's batch that was not green, by its own run.

    Args:
        own_red: Whether this landing's PR ended with its own run red. The members' places
            then go back to no answer, and the next holder may stack them.
    """
    members, ln.split_members = ln.split_members, []
    if not members:
        return
    named = " ".join(f"#{member.ln.opts.pr}" for member in members)
    if own_red:
        say(f"PR #{ln.opts.pr}'s own run is red too, so {named} go back to the line")
    else:
        say(f"PR #{ln.opts.pr}'s own run was not red, so {named} each land alone")
    for member in members:
        _answer(ln, member, "" if own_red else line.ALONE)


def _merge_commit(ln: Landing) -> str:
    """The commit `ln`'s PR merged as; "" when GitHub does not read it as merged."""
    got = ln.read(
        lambda: _api(
            ln, "GET", f"pulls/{ln.opts.pr}", jq="{merged, sha: .merge_commit_sha}"
        ),
        f"gh api pulls/{ln.opts.pr}",
    )
    pull = as_object(got, "a pull request")
    return str(pull.get("sha") or "") if pull.get("merged") is True else ""


def _merge_member(ln: Landing, member: Member, before: Landing) -> str:
    """Merge `member` behind `before`, whose PR just merged; "" on success, else why not.

    The member is labelled only once every check here has passed, and it gets a full run
    if it does not merge after that.
    """
    pr = member.ln.opts.pr
    labelled = False
    try:
        member.ln.tested_on = _merge_commit(before)
        if ln.give_back_turn is None:
            return _TURN_LOST
        if not member.ln.tested_on:
            return f"PR #{before.opts.pr}'s merge commit could not be read"
        if not any((p.pr, p.head) == (pr, member.head) for p in turn.waiting(ln)):
            return "its own landing left the line"
        head = member.ln.view("headRefOid").get("headRefOid") or ""
        if not stale_base.batch_tested(member.ln, head):
            return "the batch's run no longer stands for it"
        _label(ln, pr)
        labelled = True
        say(f"merging PR #{pr}, which the batch tested after #{before.opts.pr}")
        merge.await_merge(member.ln, lambda _draft: (True, 0))
        return ""
    except Outcome as exc:
        why = exc.error
    except (*_GH_FAILED, gh_read.GhReadFailed, OSError) as exc:
        why = str(exc)
    if labelled:
        _full_run(ln, pr)
    return why


def _merge_members(ln: Landing) -> None:
    """Merge each member the batch tested, in order, behind this landing's own merge."""
    if ln.batch and not ln.tested_on:
        say("the batch's run no longer stands, so its other PRs land alone")
        return
    # A member's wait takes twice `MEMBER_TIMEOUT_S` when its branch is updated, so merge a member
    # only while that much budget is left.
    left = 2 * ln.opts.merge_timeout - ln.merge_wait_s
    before = ln
    if ln.batch:
        ln.transitions.progress(0, len(ln.batch), "batch members merged")
    for merged, member in enumerate(ln.batch, 1):
        pr = member.ln.opts.pr
        if left < 2 * MEMBER_TIMEOUT_S:
            why = f"this landing's merge budgets have {max(left, 0)}s left"
        else:
            why = _merge_member(ln, member, before)
        if why:
            say(f"PR #{pr} is not merged ({why}); it and the PRs after it land alone")
            return
        _answer(ln, member, line.MERGED)
        ln.transitions.progress(merged, len(ln.batch), "batch members merged")
        left -= member.ln.merge_wait_s
        before = member.ln


def land(ln: Landing, take_turn: merge.TakeTurn) -> None:
    """Merge this landing's PR, and the members of the batch it formed, if it formed one.

    The batch is formed as soon as the landing takes the turn with a draft that has had no
    CI run, which is the one case `merge.await_merge` asks for the turn with a head.
    """

    def take(draft: str) -> tuple[bool, int]:
        in_turn, waited = take_turn(draft)
        if in_turn and draft:
            # Before the draft's run, so a PR its merge would be refused spends no run.
            slots.refuse_before_run(ln, draft)
            # A rate-limit wait in those rules can end without the turn (`turn.held`).
            in_turn = ln.give_back_turn is not None
        if in_turn and draft and ln.opts.batch and not ln.lands_alone:
            try:
                ln.batched_s = form(ln, draft)
            except turn.HandOver:
                raise
            except Exception as exc:
                # DECIDED: a batch never fails the landing that forms it.
                say(f"the landing batch failed ({exc!r}); PR #{ln.opts.pr} lands alone")
                ln.tested_on, ln.batch = "", []
            in_turn = ln.give_back_turn is not None
        return in_turn, waited + ln.batched_s

    own_red = False
    try:
        merge.await_merge(ln, take)
    except Outcome as exc:
        own_red = exc.verdict == Verdict.PR_CI_RED
        if ln.tested_head:
            _full_run(ln, ln.opts.pr)
        raise
    finally:
        _settle(ln, own_red)
    try:
        _merge_members(ln)
    except Exception as exc:
        # DECIDED: the same. This landing's own PR has merged, and its deploy comes next.
        say(f"the landing batch ended early ({exc!r}); its other PRs land alone")
