"""The merge phase: arm `gh pr merge --auto` here, and wait for the merge to arrive.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial.
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

GitHub's `enablePullRequestAutoMerge` mutation (what `--auto` calls) rejects a PR that is
already CLEAN -- there is nothing to defer -- so a plain `--auto` fails on exactly the PRs
that are ready to merge. A CLEAN rejection falls through to a direct `gh pr merge --squash`; a PR that
merged in the gap between the idempotency check and the `--auto` attempt is a no-op, not a
failure; anything else GitHub calls not-yet-mergeable (BLOCKED, DIRTY, ...) still dies.

`--auto` exiting 0 is not proof the merge was armed either: it can exit 0 with
`autoMergeRequest` still null, leaving the landing to poll toward merge-timeout on a PR that is
CLEAN with every check green. One read-back answers all three
questions the arm can have gone wrong in -- merged in the gap, armed, or silently not armed --
and an unarmed CLEAN PR takes the same direct-merge path a CLEAN rejection does. An unarmed
PR that is NOT CLEAN dies: direct-merging it would only fail the same way. The read-back
itself failing is not a reason to fail a landing whose arm may well have worked, so it says
so and trusts the exit code.

--await-merge polls the PR's state until merged, so `gh pr create` -> `gh pr merge --auto`
-> one backgrounded land.sh is the whole procedure.

A PR whose `reviewDecision` is REVIEW_REQUIRED is never armed. GitHub's auto-merge does not
apply a ruleset bypass, so an armed PR waiting on a review that only a bypass clears stays
BLOCKED until merge-timeout (github/docs#45265, open since 2026-07-23). `gh pr merge` refuses it
at its own pre-flight as well (cli/cli#13388). The REST merge endpoint applies the bypass, so
--await-merge merges such a PR through it once await_ci reads the head green, pinned to that
head SHA. A ruleset with no bypass actor, such as the master CI gate, still refuses that call
until its own checks pass. A refusal is reported and the wait goes on, so a caller who cannot
bypass reaches merge-timeout rather than a merge.

--arm-merge also refuses a PR whose body carries a closing keyword outside a `Closes #N` line,
before any merge call. A "Filed and not fixed: #N" line closes #N on merge, because GitHub
reads the keyword and not the sentence around it. `stray_closing_refs` owns the
rule.

--arm-merge refuses while the repo is not public, before any merge call. On the free plan GitHub
enforces no ruleset on a private repo, so `--auto` and both direct merges would go through
without the CI gate: 29 PRs did on 2026-09-21 (#3610). `_refuse_private_repo` owns it. It cannot
stop an auto-merge armed while the repo was public from firing after a later flip;
github-ruleset-drift.sh is the cover for that.

`opts.require_author` (from `LAND_REQUIRE_AUTHOR`, which renovate-agent-land@.service sets to
`app/renovate`) makes --arm-merge refuse a PR by anyone else, before any merge call. The
agent's contract says "never a PR by another author", and this is the check; an
interactive session leaves the variable unset and is unaffected. The refusal names the hand-off
rather than the `--any-author` override: only the unattended session ever reads it, and the
operator chose that a `manual —` bump goes to a person.

When a lander unit sets the landing policy (`policy.py`), --arm-merge runs its checks and pins
every merge path to the head SHA they read: `--match-head-commit` on both `gh pr merge` calls,
and `sha=` on the REST merge. --await-merge dies if the head moves before the merge, so a push
after the checks needs a re-run, which checks it again.
"""

import re
import subprocess
from enum import StrEnum

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import CI_GREEN, CI_RED
from lib.json_types import as_object
from deploy_tools.land_lib import policy
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say


class ArmDecision(StrEnum):
    """What to do after `gh pr merge --auto` refuses to arm a PR."""

    ALREADY_MERGED = "already-merged"
    MERGE_DIRECT = "merge-direct"
    DIE = "die"


def arm_merge_fallback_decision(state: str, merge_state_status: str) -> ArmDecision:
    """What to do after `gh pr merge --auto` rejects a PR: already-merged | merge-direct | die.

    GitHub's `enablePullRequestAutoMerge` mutation only accepts a PR that is genuinely
    blocked. A PR that is already CLEAN has nothing to defer, so `--auto` fails on exactly
    the PRs that are ready to merge right now. A pure function of two strings so the branch is
    testable without gh.
    """
    if state == "MERGED":
        return ArmDecision.ALREADY_MERGED
    if merge_state_status == "CLEAN":
        return ArmDecision.MERGE_DIRECT
    return ArmDecision.DIE


# GitHub's closing keywords, all three tenses of all three verbs. A keyword anywhere in the
# body, with or without a colon, closes the issue it points at when the PR merges — the
# surrounding words decide nothing, so "not fixed: #N" closes #N exactly as "Fixes #N" does.
_KEYWORD = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSING_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#(\d+)", re.IGNORECASE)
# One DELIBERATE closing reference with its trailing punctuation, so a second keyword on the
# same line is read against what is left rather than against the first reference's text.
# `Closes #2428, closes #2429` and `Closes #2412. Closes #2477:` are both ordinary here.
_CANONICAL_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#\d+[.,;:)\]]*\s*", re.IGNORECASE)
# Markdown that carries no meaning in front of a reference: list and blockquote markers,
# heading hashes, bold/italic runs, and the backticks of a code span — GitHub creates no
# reference at all inside one.
_DECORATION = re.compile(r"^[\s>*_#`~-]+|[\s>*_#`~-]+$")
_SENTENCE_END = re.compile(r"[.!?]$")


def _reference_is_deliberate(prefix: str) -> bool:
    """Whether a closing keyword preceded by `prefix` on its line is the intended kind.

    Deliberate means the keyword opens a clause of its own: it starts the line, follows
    another closing reference, or follows a finished sentence. `Filed and not fixed: #N`
    fails all three.
    """
    rest = _DECORATION.sub("", _CANONICAL_REF.sub("", prefix))
    return not rest or _SENTENCE_END.search(rest) is not None


def stray_closing_refs(body: str) -> list[str]:
    """The lines of `body` whose closing keyword is not a deliberate closing reference.

    GitHub reads the `fixed: #N` in a body that says "Filed and not fixed: #N" as a closing
    keyword, and closes the unfixed follow-up on merge, which drops it from
    `findings.py list` until someone reopens it by hand.

    Set membership cannot tell the two apart: the intentional form and the accidental one are
    the same construct, so a closing reference the PR does not intend looks exactly like one it
    does. POSITION is the discriminator — a keyword that opens a clause is the convention every
    PR body here writes, and one buried mid-sentence is the accident. A body naming an unfixed
    follow-up writes it without a keyword in front of the number: `Filed for later: #N`.

    The clause test is what the corpus forces. A keyword-opens-the-LINE rule flags the repo's
    ordinary `Closes #A. Closes #B.` on one line, and a guard that refuses a fifth of all
    landings is one the operator turns off. Flagging only a keyword that opens no clause
    avoids that.

    Known limits, both deliberate. A keyword inside a fenced code block is still flagged:
    over-flagging costs one `gh pr edit` and under-flagging costs a silently closed finding.
    And only the bare `#N` form is matched, not `owner/repo#N` or a full issue URL, which
    GitHub also closes on — no agent or template here writes either.

    A pure function of one string so the branch is testable without gh.

    Args:
        body: the PR body, as `gh pr view --json body` returns it.

    Returns:
        The stripped offending lines, in order, without duplicates.
    """
    found: list[str] = []
    for line in body.splitlines():
        for match in _CLOSING_REF.finditer(line):
            if _reference_is_deliberate(line[: match.start()]):
                continue
            if line.strip() not in found:
                found.append(line.strip())
    return found


def _refuse_stray_closing_refs(ln: Landing, body: str) -> None:
    """Die when the PR body carries a closing reference that is not its own `Closes #N` line.

    Runs beside `_require_author`, before any merge call, because the damage happens AT the
    merge and is not undone by one: reopening the issue is a hand step the operator only takes
    once they notice. The session that hits this is usually not the session that wrote the
    body — the fan-out agent opens the PR on daniel-server and daniel-box lands it — so the
    message says what to rewrite rather than assuming the reader chose the wording.
    """
    stray = stray_closing_refs(body)
    if not stray:
        return
    lines = "\n  ".join(stray)
    ln.die(
        f"PR #{ln.opts.pr}'s body carries a closing keyword that is not a `Closes #N` line, "
        f"so merging it closes an issue this PR may not have fixed (issue #2513):\n"
        f"  {lines}\n"
        "Rewrite the reference without a closing keyword in front of the number — "
        '"Filed for later: #N" — or move a deliberate close onto its own `Closes #N` line, '
        "then `gh pr edit` the body and re-run this.",
        1,
    )


def _pin(ln: Landing) -> list[str]:
    """`gh pr merge`'s head pin when the landing policy checked a head; else nothing."""
    return ["--match-head-commit", ln.pinned_head] if ln.pinned_head else []


def _merge_direct(ln: Landing, subject: str) -> None:
    """Squash-merge the PR now, for a PR `--auto` will not or did not arm."""
    say(f"PR #{ln.opts.pr} is CLEAN -- nothing to defer; merging directly")
    try:
        ln.tools.gh(
            "pr", "merge", ln.opts.pr, "--squash", "--subject", subject, *_pin(ln)
        )
    except subprocess.CalledProcessError as exc:
        ln.die(
            f"direct gh pr merge --squash failed for PR #{ln.opts.pr}: {exc.stderr.strip()}",
            1,
        )
    say(f"merged directly: {subject}")


def _leave_for_a_direct_merge(ln: Landing, subject: str) -> None:
    """Hand a PR blocked by a missing review to await_merge, which merges it directly.

    Arming `--auto` here would leave it BLOCKED until merge-timeout; the module docstring has
    why. Without --await-merge nothing in this run would merge it, so that dies instead.
    """
    pr = ln.opts.pr
    if not ln.opts.await_merge:
        ln.die(
            f"PR #{pr} waits on a review that only a ruleset bypass clears, and auto-merge "
            "never applies a bypass — re-run with both --arm-merge --await-merge, which "
            "merges it directly once CI is green; --await-merge alone only polls",
            1,
        )
    ln.direct_merge_subject = subject
    say(
        f"PR #{pr} waits on a review; not arming auto-merge, which ignores a ruleset "
        "bypass — merging directly once CI is green"
    )


def _merge_past_review(ln: Landing, head: str) -> str:
    """Squash-merge the PR at `head` through the REST endpoint; '' on success, else why not.

    The REST endpoint is the merge path that applies a ruleset bypass (module docstring).
    `sha` makes GitHub refuse if the head moved since await_ci read it green.
    `{owner}/{repo}` resolves from the checkout, as every `gh pr view` here does.
    """
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/merge",
            "-f",
            "merge_method=squash",
            "-f",
            f"sha={head}",
            "-f",
            f"commit_title={ln.direct_merge_subject}",
        )
    except subprocess.CalledProcessError as exc:
        return (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
    except subprocess.TimeoutExpired:
        return "gh timed out"
    say(f"merged directly past the review requirement: {ln.direct_merge_subject}")
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
    try:
        repo = as_object(
            ln.tools.gh_json("api", "repos/{owner}/{repo}") or {}, "gh api repos"
        )
    except subprocess.CalledProcessError as exc:
        ln.die(f"could not read the repo's visibility: {exc.stderr.strip()}", 1)
    except subprocess.TimeoutExpired:
        ln.die("could not read the repo's visibility: gh timed out", 1)
    except ValueError:
        ln.die("could not read the repo's visibility: unparseable gh output", 1)
    visibility = repo.get("visibility") or "<unknown>"
    if visibility != "public":
        ln.die(
            f"repo is {visibility}: rulesets are not enforced on the free plan, so the CI "
            f"merge gate is open — not merging PR #{ln.opts.pr} until the repo is public "
            "again",
            1,
        )


def arm_merge(ln: Landing) -> None:
    """Run `gh pr merge --squash --auto` for this PR, unless it is already merged."""
    pr = ln.opts.pr
    view = ln.view("state,title,body,reviewDecision")
    if view.get("state") == "MERGED":
        say("already merged; --arm-merge is a no-op")
        return
    if view.get("state") == "CLOSED":
        ln.die(f"PR #{pr} was closed without merging — nothing to arm", 1)
    _require_author(ln)
    _refuse_stray_closing_refs(ln, view.get("body") or "")
    _refuse_private_repo(ln)
    if ln.opts.policy_active:
        ln.pinned_head = policy.check(ln)
    subject = ln.opts.subject or view.get("title", "")
    if view.get("reviewDecision") == "REVIEW_REQUIRED":
        _leave_for_a_direct_merge(ln, subject)
        return
    try:
        ln.tools.gh(
            "pr", "merge", pr, "--squash", "--auto", "--subject", subject, *_pin(ln)
        )
    except subprocess.CalledProcessError:
        retry = ln.view("state,mergeStateStatus")
        decision = arm_merge_fallback_decision(
            retry.get("state", ""), retry.get("mergeStateStatus", "")
        )
        if decision == ArmDecision.ALREADY_MERGED:
            # A race with the idempotency check above: the PR merged between that read and
            # this --auto attempt. Keep the MERGED short-circuit's semantics: say, not die.
            say(f"gh pr merge --auto failed because PR #{pr} merged in the meantime")
            return
        if decision == ArmDecision.MERGE_DIRECT:
            _merge_direct(ln, subject)
            return
        ln.die(
            f"gh pr merge --auto failed for PR #{pr} "
            f"(mergeStateStatus={retry.get('mergeStateStatus', '')})",
            1,
        )
    # --auto exiting 0 is not proof the merge was armed. One read-back
    # answers every way it can have gone wrong; a read-back that itself fails must not turn
    # a possibly-successful arm into a failed landing. Only the read is guarded: an Outcome
    # raised by anything after it is a real verdict and must not be swallowed here.
    try:
        armed = ln.view("state,mergeStateStatus,autoMergeRequest")
    except Outcome:
        say(f"could not confirm PR #{pr}'s arm; trusting gh pr merge --auto's exit 0")
        return
    state = armed.get("state", "")
    mss = armed.get("mergeStateStatus", "")
    if state == "MERGED":
        say(f"PR #{pr} merged in the meantime")
        return
    if state == "OPEN" and armed.get("autoMergeRequest") is None:
        if arm_merge_fallback_decision(state, mss) == ArmDecision.MERGE_DIRECT:
            say(
                f"gh pr merge --auto exited 0 but PR #{pr} is not armed "
                "(autoMergeRequest is null)"
            )
            _merge_direct(ln, subject)
            return
        ln.die(
            f"gh pr merge --auto exited 0 but PR #{pr} is not armed "
            f"(mergeStateStatus={mss})",
            1,
        )
    say(f"auto-merge armed: {subject}")


def await_merge(ln: Landing) -> None:
    """Poll until merged. Bail early only on the two states an auto-merge never leaves.

    Only CONFLICTING may bail, and only on two consecutive polls: GitHub computes
    mergeability asynchronously and serves UNKNOWN until it settles, and master moving under
    the PR flips the field for one poll. A red PR
    CI is the other way an armed auto-merge never fires; GitHub says only `BLOCKED`, the
    same word it uses while checks run, so await_ci owns that verdict, one-shot. Only its
    exit 1 bails: `pending` IS the grace period, derived rather than guessed.

    A PR arm_merge left for a direct merge is merged here, on the first poll where await_ci
    reads its head green.
    """
    o, t = ln.opts, ln.tools
    waited = 0
    conflicting = 0
    last_refusal = ""
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
                f"PR #{o.pr} conflicts with {BRANCH} — rebase it, re-arm "
                "`gh pr merge --squash --auto`, then re-run this",
                1,
                Verdict.MERGE_CONFLICT,
            )
        head = view.get("headRefOid") or ""
        if ln.pinned_head and head and head != ln.pinned_head:
            ln.die(
                f"PR #{o.pr}'s head moved from {ln.pinned_head[:8]} to {head[:8]} after the "
                "landing policy checked it; re-run so the new head is checked",
                1,
            )
        if head:
            rc, line = t.await_ci(head, 0)
            if rc == CI_RED:
                ln.die(
                    f"PR #{o.pr} cannot merge — its own CI is red ({line}); fix it, push, "
                    "and re-run this",
                    1,
                    Verdict.PR_CI_RED,
                )
            if rc == CI_GREEN and ln.direct_merge_subject:
                refusal = _merge_past_review(ln, head)
                if not refusal:
                    continue
                if refusal != last_refusal:
                    say(
                        f"GitHub refused the direct merge at {head[:8]}; waiting: {refusal}"
                    )
                    last_refusal = refusal
        if waited >= o.merge_timeout:
            ln.die(
                f"PR #{o.pr} still {state} after {o.merge_timeout}s — not being merged; "
                "look at its checks or the queue",
                75,
                Verdict.MERGE_TIMEOUT,
            )
        t.sleep(o.merge_poll)
        waited += o.merge_poll
    say(f"merged after {waited}s")
