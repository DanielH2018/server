"""The merge phase: arm `gh pr merge --auto` here, and wait for the merge to arrive.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial (three attempts, three denials, 2026-09-03, issue #979).
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

GitHub's `enablePullRequestAutoMerge` mutation (what `--auto` calls) rejects a PR that is
already CLEAN -- there is nothing to defer -- so --arm-merge used to fail on exactly the PRs
that were ready to merge (issue #1008, reproduced on PRs #998/#1001/#1002/#1004 on
2026-09-03). A CLEAN rejection falls through to a direct `gh pr merge --squash`; a PR that
merged in the gap between the idempotency check and the `--auto` attempt is a no-op, not a
failure; anything else GitHub calls not-yet-mergeable (BLOCKED, DIRTY, ...) still dies.

`--auto` exiting 0 is not proof the merge was armed either (issue #1029): on PR #1026 it
exited 0, `autoMergeRequest` stayed null, and the landing polled 35 minutes toward
merge-timeout on a PR that was CLEAN with every check green. One read-back answers all three
questions the arm can have gone wrong in -- merged in the gap, armed, or silently not armed --
and an unarmed CLEAN PR takes the same direct-merge path a CLEAN rejection does. An unarmed
PR that is NOT CLEAN dies: direct-merging it would only fail the same way. The read-back
itself failing is not a reason to fail a landing whose arm may well have worked, so it says
so and trusts the exit code.

--await-merge polls the PR's state until merged, so `gh pr create` -> `gh pr merge --auto`
-> one backgrounded land.sh is the whole procedure. Every landing on 2026-09-01 hand-wrote
that wait.

--arm-merge also refuses a PR whose body carries a closing keyword outside a `Closes #N` line,
before any merge call. A "Filed and not fixed: #N" line closes #N on merge, because GitHub
reads the keyword and not the sentence around it (issue #2513). `stray_closing_refs` owns the
rule.

`opts.require_author` (from `LAND_REQUIRE_AUTHOR`, which renovate-agent.service sets to
`app/renovate`) makes --arm-merge refuse a PR by anyone else, before any merge call. The
agent's contract said "never a PR by another author" and nothing checked (#2170); an
interactive session leaves the variable unset and is unaffected.
"""

import re
import subprocess
from enum import StrEnum

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.exit_codes import CI_RED
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
    the PRs that are ready to merge right now (issue #1008; PRs #998, #1001, #1002, #1004 on
    2026-09-03). A pure function of two strings so the branch is testable without gh.
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
    fails all three, which is the accident issue #2513 is about.
    """
    rest = _DECORATION.sub("", _CANONICAL_REF.sub("", prefix))
    return not rest or _SENTENCE_END.search(rest) is not None


def stray_closing_refs(body: str) -> list[str]:
    """The lines of `body` whose closing keyword is not a deliberate closing reference.

    PR #2510's body said "Filed and not fixed: #2509". GitHub read `fixed: #2509` as a closing
    keyword and closed the unfixed follow-up two seconds after the merge, which dropped it from
    `findings.py list` until the operator reopened it by hand (issue #2513).

    Set membership cannot tell the two apart: the intentional form and the accidental one are
    the same construct, so a closing reference the PR does not intend looks exactly like one it
    does. POSITION is the discriminator — a keyword that opens a clause is the convention every
    PR body here writes, and one buried mid-sentence is the accident. A body naming an unfixed
    follow-up writes it without a keyword in front of the number: `Filed for later: #N`.

    The clause test is what the corpus forced. Read against 60 merged PR bodies on 2026-09-24,
    a keyword-opens-the-LINE rule flagged 13, of which 12 were the repo's ordinary
    `Closes #A. Closes #B.` on one line — a guard that refuses a fifth of all landings is one
    the operator turns off. Flagging only a keyword that opens no clause leaves exactly PR
    #2510 flagged out of those 60.

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


def _merge_direct(ln: Landing, subject: str) -> None:
    """Squash-merge the PR now, for a PR `--auto` will not or did not arm."""
    say(f"PR #{ln.opts.pr} is CLEAN -- nothing to defer; merging directly")
    try:
        ln.tools.gh("pr", "merge", ln.opts.pr, "--squash", "--subject", subject)
    except subprocess.CalledProcessError as exc:
        ln.die(
            f"direct gh pr merge --squash failed for PR #{ln.opts.pr}: {exc.stderr.strip()}",
            1,
        )
    say(f"merged directly: {subject}")


def _require_author(ln: Landing) -> None:
    """Die unless the PR's author is `opts.require_author`; a no-op when it is unset.

    Its own `gh pr view --json author` rather than a field on the state read, so a session
    with no author requirement makes exactly the calls it made before. `login` is what
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
            f"{want}'s PRs (LAND_REQUIRE_AUTHOR); a session allowed to merge it passes "
            "--any-author",
            1,
        )


def arm_merge(ln: Landing) -> None:
    """Run `gh pr merge --squash --auto` for this PR, unless it is already merged."""
    pr = ln.opts.pr
    view = ln.view("state,title,body")
    if view.get("state") == "MERGED":
        say("already merged; --arm-merge is a no-op")
        return
    if view.get("state") == "CLOSED":
        ln.die(f"PR #{pr} was closed without merging — nothing to arm", 1)
    _require_author(ln)
    _refuse_stray_closing_refs(ln, view.get("body") or "")
    subject = ln.opts.subject or view.get("title", "")
    try:
        ln.tools.gh("pr", "merge", pr, "--squash", "--auto", "--subject", subject)
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
    # --auto exiting 0 is not proof the merge was armed (issue #1029). One read-back
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
    mergeability asynchronously and serves UNKNOWN until it settles (PR #657 read UNKNOWN on
    a live open PR), and master moving under the PR flips the field for one poll. A red PR
    CI is the other way an armed auto-merge never fires; GitHub says only `BLOCKED`, the
    same word it uses while checks run, so await_ci owns that verdict, one-shot. Only its
    exit 1 bails: `pending` IS the grace period, derived rather than guessed.
    """
    o, t = ln.opts, ln.tools
    waited = 0
    conflicting = 0
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
        if head:
            rc, line = t.await_ci(head, 0)
            if rc == CI_RED:
                ln.die(
                    f"PR #{o.pr} cannot merge — its own CI is red ({line}); fix it, push, "
                    "and re-run this",
                    1,
                    Verdict.PR_CI_RED,
                )
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
