#!/usr/bin/env python3
"""File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues.

WHY A WRAPPER. The homelab-review skill, the review-and-fix command and an ordinary session all produce findings
nobody fixes that day. GitHub Issues has the status field; this script owns the three rules that make issues a
register rather than a pile: one issue per fingerprint, a re-observation is a comment and the third one
escalates, and a refuted finding stays closed.

Every command PLANS a list of gh argv first (pure, unit-tested), then runs it. `--dry-run`
prints the plan and writes nothing.

VERIFY-BY. `open --verify-by '<how to check it>'` stores prose in the issue body under a `## Verify-by` heading;
`verify` prints those descriptions back so a person or an agent can act on them. NOTHING IS EXECUTED. Prose
describes any check, including the ones no safe command can express, and it removes the need to execute text read
out of a GitHub issue body. A verify-by written before that change is still read back correctly: the fence around
it is stripped and the command shown as the instruction it always was.

This file is the CLI: the `cmd_*` handlers and the exit contract. Argument parsing is
`findings_lib/cli.py`, the vocabulary and the pure reads are `findings_lib/issue_model.py`, the gh argv are
`findings_lib/plans.py`, the gh calls are `findings_lib/gh_calls.py`, claim staleness is `findings_lib/claim.py`,
the four claim subcommands are `findings_lib/claim_cli.py`, and verify-by is `findings_lib/verify.py`.

Usage (every subcommand also takes `--repo OWNER/NAME`; see below)::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] \\
        [--verify-by 'Run probe.py health <svc>; it should exit 0.'] \\
        [--not-before 2026-09-12] [--manual] [--repo DanielH2018/dotfiles] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py comment 688 --body-file note.md
    uv run python scripts/dev/findings.py defer 688 --until 2026-09-12
    uv run python scripts/dev/findings.py defer 688 --clear
    uv run python scripts/dev/findings.py manual 688 [--clear]
    uv run python scripts/dev/findings.py claim 688 701 --worktree worktree-foo \\
        [--session id] [--force]
    uv run python scripts/dev/findings.py release 688 --worktree worktree-foo [--reason "..."]
    uv run python scripts/dev/findings.py claims [--worktree <branch>] [--all] [--json]
    uv run python scripts/dev/findings.py reap [--dry-run]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py close 688 --accepted --reason "..."
    uv run python scripts/dev/findings.py list [--severity S] [--domain D] [--limit N] [--json]
    uv run python scripts/dev/findings.py history <terms> [--file path] [--limit N] [--json]
    uv run python scripts/dev/findings.py show 688 [--full] [--json]
    uv run python scripts/dev/findings.py export --out register.json
    uv run python scripts/dev/findings.py verify --all
    uv run python scripts/dev/findings.py verify 688 701
    uv run python scripts/dev/findings.py next [--limit N] [--waiting] [--json]
    uv run python scripts/dev/findings.py sync-board [688 701] [--dry-run]

THE PROJECT BOARD FOLLOWS THE ISSUE. After any subcommand that writes an issue, `main` makes
that issue's item on the "Server Issue Tracking" Project agree with it: Status (Todo, In
Progress while `claimed` or while an open PR will close it, Done once closed), Severity and
Not before. `findings_lib/board.py` owns the mapping. A board write that fails prints one
warning and leaves the exit code alone, because nothing reads the board back; `sync-board`
repairs it, and with no numbers it checks every open issue. Another repo's register has no
board, so `--repo` other than this one skips it.

WORKING ANOTHER REPO'S REGISTER. Every subcommand takes `--repo OWNER/NAME` and then works
that repo's own `claude` register, with the same labels, trailer, fingerprint dedup and claim
protocol it has here. Every gh call carries the flag, reads included: a dedup read against
this repo while the create went elsewhere would re-file the finding on every run, and issue
numbers collide across repos, so `next` reading this repo's PRs would withhold dotfiles #750
for a server PR that closes #750. `claim`, `claims`, `reap` and `next` also judge each claim
against that repo's local checkout and its default branch (`REGISTER_CHECKOUTS` in
`findings_lib/boundaries.py`), because a claim names a branch in the repo that owns the issue.
They refuse a repo that table does not list, with exit 2. `issue-fanout` has the dotfiles
route end to end.

CLOSING A FINDING. `--fixed` closes as completed. The other two close as not planned and are
terminal, so `open` refuses to re-file the same fingerprint afterwards: `--refuted` records
that a skeptic disproved it, and `--accepted` records that it is TRUE and the operator chose
to live with the trade-off. Both need `--reason`. Reach for `--accepted` rather than closing
by hand — a hand-close is invisible to the dedup, so the next review re-files an accepted
decision and the comment on it reads "treat as a regression".

RELEASING A STRANDED CLAIM. Every path that ends or restarts an issue's life releases the claim on it first,
whoever holds it — `close` and `open`'s reopen path alike, both through `plan_release_held`. `claims`, `reap` and
`next` read OPEN issues, so a claim left on a closed one is invisible to every view at once rather than merely
wrong, and a reopen brings it back LIVE.

CLAIMING AN ISSUE. `claim` posts a `Claim:` comment and adds the `claimed` label, so a
worktree fanning out several issues at once knows which are its own; `release` reverses
that. A claim only counts from the operator's own comment — this repo is public, so any
account can post a `Claim:` or `Released:` trailer and none of them decide anything. `claim`
refuses an issue another worktree LIVE-holds, refuses `manual` issues, closed issues and
issues outside the `claude` register, RELEASES a claim it finds stale and takes the issue,
and treats re-claiming its own claim as a no-op; `release` refuses any claim but its own.
Both repair a label that disagrees with the comments, in either direction. Before it writes
anything, `claim` checks that `--worktree` would not read STALE the moment the claim lands,
and refuses with `--force` named unless it is passed. `claims` lists every claim, warning on
stderr (but still rendering) if the worktree read failed; `reap` refuses outright on that
same failure, and `claim` leaves a stale claim standing rather than reaping on a guess.
`claim` takes every issue it can and refuses the rest, so one `manual` issue does not cost
the good claims in the same batch.

PICKING UP WORK. `next` prints EVERY issue a session may claim, best severity first,
withholding `manual` issues, issues deferred to a later date, issues an open issue blocks, issues a LIVE claim already
holds, and issues an open PR already says it closes. An issue whose claim is stale IS
offered, marked with who holds it — `claim` reaps that claim on the way past, and `reap`
clears every one of them at once. `--limit N` bounds the list; there is no default bound,
because one truncated the free set silently and the reader took ten rows for all of them.

DEFERRING AN ISSUE. `defer <n> --until <date>` (or `open --not-before <date>`) puts a `not-before:<YYYY-MM-DD>`
label on the issue. `next` and `claim` withhold it while today is before that date and offer it ON the date, with
nothing to clear: the label expires by comparison, which `manual` cannot. `next` still names a deferred issue —
under a `deferred:` line, and on stderr under `--json` so the array stays the free set — because a silently
withheld issue reads as a closed one to whoever looks at the backlog. `defer <n> --clear` lifts it early.

BLOCKING AN ISSUE ON OTHER WORK. `block <n> --on <m>` (or `open --blocked-by <m>`) records GitHub's native
"blocked by" relationship. `next` and `claim` withhold the issue while any blocker is open, and it frees itself the
moment the last one closes, so use it rather than a guessed date when the issue waits on another issue or on the
PR that closes one. A set of changes with no issue gets one filed first. `next` names a blocked issue under a
`blocked:` line, as it names a deferred one. A block and a date can both apply, as for work that waits on an
issue and then on a retention period, and `next` offers the issue only once both have passed.
`block <n> --clear` removes every blocker now, or only those `--on` names. When a body says the finding waits on
`#<m>` or a slice code ("once R4 retires", "after H7.53"), `open` and `defer` print a stderr hint naming the issue
and the `block` command; a slice resolves to the issue titled `<code>:`, and a closed one is called out instead.

RESERVING AN ISSUE FOR THE OPERATOR. `manual <n>` (or `open --manual`) adds the `manual`
label: `next` withholds the issue, `claim` refuses it, and `list` still marks it `[manual]`.
Unlike a date, it never expires; `manual <n> --clear` removes it. Marking an issue manual also
releases any claim on it, because `reap` skips `manual` issues and would never clear that
claim. `open --manual` labels an issue the dedup matched as well as a new one, so the flag is
never dropped silently.

Exit codes: 0 done; 1 gh failed, or `reap` refused a git read failure rather than call it
"nothing is claimed"; 64 bad arguments, which includes a `--worktree` name the claim trailer
could not carry; 3 nothing was written because the issue refuses it — closed, `manual`,
deferred to a later date, blocked by an open issue, outside the register, held by another worktree, not claimed, or
lost a race to another claim — or because `claim`'s own `--worktree` would read stale at
birth, or because `manual` found the label already in the state it was asked for.
"""

import argparse
import dataclasses
import json
import subprocess
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# DECIDED: the leaves are imported as `dev.findings_lib.<leaf>`, never as bare siblings.
# `scripts/docs/reference/backlog.py` reaches this code as `dev.findings` with only
# `scripts/` on sys.path, so a bare `from issue_model import ...` would raise
# ModuleNotFoundError under the docs-refresh cron while pytest stayed green.
from dev.findings_lib.board import FAILED, UNCHANGED, UPDATED, board_warning
from dev.findings_lib.claim import claim_states
from lib.exit_codes import FINDINGS_BAD_ARGS
from lib.progress import count_line
from dev.findings_lib.claim_cli import cmd_claim, cmd_claims, cmd_reap, cmd_release
from dev.findings_lib.cli import _parser
from dev.findings_lib.export_cli import cmd_export
from dev.findings_lib.history_cli import cmd_history, cmd_show
from dev.findings_lib.gh_calls import (
    _create,
    _existing_labels,
    _load_issue,
    created_number,
    fingerprint_match,
    load_issues,
    blocker_hints,
    open_pr_refs,
    run,
)
from dev.findings_lib.issue_model import (
    NO_REOPEN,
    PR_REPO,
    cited_paths,
    current_claim,
    recorded_blockers,
    is_deferred_row,
    now_iso,
    fingerprint,
    issue_rows,
    label_names,
    not_before_label,
    pickable,
    sort_key,
    today_utc,
    waiting,
    waiting_line,
)
from dev.findings_lib.plans import (
    ClaimRefused,
    plan_block,
    plan_close,
    plan_comment,
    plan_defer,
    plan_ensure_label,
    plan_manual,
    plan_release_held,
    plan_open,
    plan_sync_labels,
    plan_touch,
)
from dev.findings_lib.boundaries import (
    REGISTER_CHECKOUTS,
    FindingsTools,
    aimed,
    read_peer_worktrees,
)
from dev.findings_lib.red_green import RED_GREEN_LABEL, red_green_eligible
from dev.findings_lib.solo_only import solo_only_reasons
from dev.findings_lib.verify import verification_report


def cmd_open(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``open`` subcommand: files, touches or reopens a finding's issue.

    Syncs labels first since ``gh issue create --label`` fails on a label the repo lacks,
    then reads the body file, computes the fingerprint, and runs whatever ``plan_open``
    decides.

    Args:
        args: parsed CLI namespace for the ``open`` subcommand.
        tools: the process boundaries every gh call goes through.

    Returns:
        The process exit code: 0 on success, 64 if the body file is missing, 3 if the
        fingerprint belongs to an issue closed as refuted or accepted.
    """
    if not args.body_file.is_file():
        sys.stderr.write(f"open: body file not found: {args.body_file}\n")
        return FINDINGS_BAD_ARGS
    body = args.body_file.read_text()
    blockers = _split_refs(args.blocked_by)
    fp = fingerprint(args.title, args.file)
    labels = ["claude", f"severity/{args.severity}", f"kind/{args.kind}"]
    if args.domain:
        labels.append(f"domain/{args.domain}")
    if args.no_vetted_remediation:
        labels.append("no-vetted-remediation")
    if args.manual:
        labels.append("manual")
    if args.review_leftover:
        labels.append("review-leftover")
    if tools.repo is None and red_green_eligible(cited_paths(body)):
        labels.append(RED_GREEN_LABEL)  # a --review fan-out runs a red phase (#3950)
    # `gh issue create --label` fails on a label the repo lacks, so `open` creates LABELS
    # first, and a dated label or `red-green` the first time it is used.
    have = _existing_labels(tools)
    extra = [not_before_label(args.not_before)] if args.not_before else []
    extra += [RED_GREEN_LABEL] if RED_GREEN_LABEL in labels else []
    ensure = [plan for name in extra for plan in plan_ensure_label(name, have)]
    run(plan_sync_labels(have) + ensure, args.dry_run, tools)
    existing = fingerprint_match(fp, tools)
    outcome, code, plans = plan_open(
        existing,
        title=args.title,
        body=body,
        labels=labels,
        fp=fp,
        source=args.source,
        verify_by=args.verify_by,
        defer_until=args.not_before,
    )
    if outcome == "created":
        if args.dry_run:
            run(plans, True, tools)
            if blockers:
                print(f"gh issue edit <new> --add-blocked-by {','.join(blockers)}")
            print(f"(dry-run) would create; fingerprint {fp}")
            _print_blocker_hints(body, "<new>", blockers, tools)
            return 0
        url = _create(plans[0], tools)
        number = url.rsplit("/", 1)[-1]
        if blockers:
            # `gh issue create` has no blocked-by flag, so the relationship is a second write.
            run(plan_block({"number": number}, on=blockers, clear=False), False, tools)
        print(f"#{number} created  {url}")
        _print_blocker_hints(body, number, blockers, tools)
        return 0
    # "created" is the only outcome plan_open returns for a missing issue, so every branch
    # below has one to name. Checked here rather than in each branch: the two of them read
    # `existing` four times between them.
    assert existing is not None
    if outcome in NO_REOPEN:
        print(
            f"#{existing['number']} {outcome}: closed on "
            f"{(existing.get('closedAt') or '?')[:10]}; not reopened"
        )
        return code
    if outcome == "reopened":
        # A `Closes #<n>` merge closes an issue without going through `close`, so the claim
        # is still on it. Reopening for a later re-observation brings that claim back LIVE,
        # where it blocks `claim` and withholds the issue from `next` for as long as the
        # claiming worktree exists — an orchestrator's can be a long time. Released
        # as its OWN comment rather than folded into the regression note, so the body never
        # carries two claim trailers at once (see `current_claim`'s DECIDED marker).
        plans += plan_release_held(
            existing, when=now_iso(), reason="reopened after a re-observation"
        )
    if blockers:
        plans += plan_block({**existing, "state": "OPEN"}, on=blockers, clear=False)
    if args.manual and "manual" not in label_names(existing):
        # A matched issue would otherwise drop the flag silently. A reopened one is open by
        # the time these run, and its claim was released just above.
        plans += plan_manual({**existing, "state": "OPEN"}, clear=False)
        if outcome != "reopened":
            plans += _release_for_manual(existing)
    run(plans, args.dry_run, tools)
    print(f"#{existing['number']} {outcome}  {existing.get('url', '')}")
    return 0


def cmd_touch(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``touch`` subcommand: records a re-observation on an open issue.

    Args:
        args: parsed CLI namespace carrying ``number``, ``source`` and ``dry_run``.
        tools: the process boundaries every gh call goes through.

    Returns:
        3 if the issue is already closed, 0 otherwise.
    """
    issue = _load_issue(args.number, tools)
    if issue.get("state") == "CLOSED":
        terminal = sorted(NO_REOPEN & label_names(issue))
        why = terminal[0] if terminal else "fixed"
        print(f"#{args.number} is closed ({why}); use open to re-file", file=sys.stderr)
        return 3
    plans = plan_touch(issue, args.source)
    run(plans, args.dry_run, tools)
    escalated = any(p[:2] == ["issue", "edit"] for p in plans)
    print(f"#{args.number} touched{' and escalated' if escalated else ''}")
    return 0


def cmd_comment(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``comment`` subcommand: posts a plain comment on a finding.

    Returns:
        FINDINGS_BAD_ARGS if the body file is missing, 3 if `plan_comment` refuses the
        issue or body, 0 otherwise.
    """
    if not args.body_file.is_file():
        sys.stderr.write(f"comment: body file not found: {args.body_file}\n")
        return FINDINGS_BAD_ARGS
    issue = _load_issue(args.number, tools)
    try:
        plans = plan_comment(issue, args.body_file.read_text())
    except ClaimRefused as exc:
        print(f"#{args.number} refused: {exc.reason}", file=sys.stderr)
        return 3
    run(plans, args.dry_run, tools)
    print(f"#{args.number} commented")
    return 0


def cmd_defer(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``defer`` subcommand: sets, moves or clears an issue's not-before date.

    Returns:
        3 if the issue is closed, or ``--clear`` finds no deferral to clear; 0 otherwise.
    """
    issue = _load_issue(args.number, tools)
    try:
        plans = plan_defer(
            issue, until=args.until, existing_labels=_existing_labels(tools)
        )
    except ClaimRefused as exc:
        print(f"#{args.number} refused: {exc.reason}", file=sys.stderr)
        return 3
    run(plans, args.dry_run, tools)
    what = (
        "deferral cleared" if args.clear else f"deferred until {args.until.isoformat()}"
    )
    print(f"#{args.number} {what}")
    if not args.clear:
        _print_blocker_hints(
            str(issue.get("body") or ""), args.number, recorded_blockers(issue), tools
        )
    return 0


def _print_blocker_hints(
    body: str, own: int | str, blocked_by: list[str], tools: FindingsTools
) -> None:
    """Prints `blocker_hints` to stderr; a failed lookup costs the hint, never the write."""
    try:
        lines = blocker_hints(body, own, blocked_by, tools)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"hint: could not look up what the body waits on: {exc}\n")
        return
    for line in lines:
        sys.stderr.write(line + "\n")


def cmd_block(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``block`` subcommand: marks an issue blocked by others, or removes that.

    Returns:
        FINDINGS_BAD_ARGS if neither ``--on`` nor ``--clear`` was given; 3 if the issue is
        closed, names itself, or ``--clear`` finds no blocker; 0 otherwise.
    """
    on = _split_refs(args.on)
    if not on and not args.clear:
        sys.stderr.write("block: need --on <issue>, or --clear\n")
        return FINDINGS_BAD_ARGS
    issue = _load_issue(args.number, tools)
    try:
        plans = plan_block(issue, on=on, clear=args.clear)
    except ClaimRefused as exc:
        print(f"#{args.number} refused: {exc.reason}", file=sys.stderr)
        return 3
    run(plans, args.dry_run, tools)
    what = "unblocked from" if args.clear else "blocked by"
    print(f"#{args.number} {what} {plans[0][-1].replace(',', ', ')}")
    return 0


def _split_refs(values: list[str] | None) -> list[str]:
    """``--on 1,2 --on 3`` as ``["1", "2", "3"]``; gh takes either spelling."""
    return [v.strip() for value in values or [] for v in value.split(",") if v.strip()]


def _release_for_manual(issue: dict) -> list[list[str]]:
    # Load-bearing: `reap` skips `manual` issues (`another_claim_blocks`), so a claim left on
    # one would never be cleared.
    return plan_release_held(issue, when=now_iso(), reason="marked manual")


def cmd_manual(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``manual`` subcommand: reserves an issue for the operator, or clears that.

    Returns:
        3 if the issue is closed, already `manual`, or ``--clear`` finds no label; 0 otherwise.
    """
    issue = _load_issue(args.number, tools)
    try:
        plans = plan_manual(issue, clear=args.clear)
    except ClaimRefused as exc:
        print(f"#{args.number} refused: {exc.reason}", file=sys.stderr)
        return 3
    if not args.clear:
        plans += _release_for_manual(issue)
    run(plans, args.dry_run, tools)
    print(f"#{args.number} {'manual cleared' if args.clear else 'marked manual'}")
    return 0


def cmd_close(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``close`` subcommand: closes an issue as fixed, refuted or accepted.

    Args:
        args: parsed CLI namespace for the ``close`` subcommand.
        tools: the process boundaries every gh call goes through.

    Returns:
        2 if ``--pr`` is combined with anything but ``--fixed``, or ``--reason`` is missing
        from a not-planned close; 0 otherwise.
    """
    outcome = "fixed" if args.fixed else "refuted" if args.refuted else "accepted"
    # argparse cannot express "--pr only with --fixed" across a mutually exclusive group.
    if outcome != "fixed" and args.pr:
        sys.stderr.write("close --pr goes with --fixed\n")
        return FINDINGS_BAD_ARGS
    if outcome != "fixed" and not args.reason:
        sys.stderr.write(
            f"close --{outcome} needs --reason: a bare verdict teaches the next run nothing\n"
        )
        return FINDINGS_BAD_ARGS
    if outcome != "fixed":
        # `gh issue edit --add-label` fails on a label the repo does not have, and the
        # not-planned outcomes are the only close that applies one. `refuted` exists only
        # because some earlier `open` created it; `accepted` would not on its first use.
        run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    # Closing ends the work, so it releases whoever holds the claim — not just the caller's
    # own worktree. `claims`, `reap` and `next` all read open issues, so a claim left on a
    # closed issue disappears from every view at once rather than showing up wrong.
    issue = _load_issue(args.number, tools)
    plans = plan_release_held(issue, when=now_iso(), reason=f"closed as {outcome}")
    plans += plan_close(args.number, outcome=outcome, pr=args.pr, reason=args.reason)
    run(plans, args.dry_run, tools)
    print(f"#{args.number} closed as {outcome}")
    return 0


def cmd_verify(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``verify`` subcommand: prints how to check each finding, and runs nothing.

    A verify-by is prose, so this command reports and does not decide. It executes no stored
    text, reaches no verdict, and closes nothing — closing stays with `close`, where a human
    or an agent chooses it after doing what the instructions describe.

    Args:
        args: parsed CLI namespace carrying ``all`` and ``numbers``.
        tools: the process boundaries the issue reads go through.

    Returns:
        64 if neither or both of ``--all``/issue numbers were given, 0 otherwise.
    """
    if args.all and args.numbers:
        sys.stderr.write("verify: pass --all or issue numbers, not both\n")
        return FINDINGS_BAD_ARGS
    if not args.all and not args.numbers:
        sys.stderr.write("verify: need --all or at least one issue number\n")
        return FINDINGS_BAD_ARGS
    issues = (
        load_issues("open", tools)
        if args.all
        else [_load_issue(n, tools) for n in args.numbers]
    )
    print(verification_report(issues))
    return 0


def cmd_sync_labels(args: argparse.Namespace, tools: FindingsTools) -> int:
    plans = plan_sync_labels(_existing_labels(tools))
    run(plans, args.dry_run, tools)
    print(f"sync-labels: {len(plans)} label(s) created")
    return 0


def cmd_sync_board(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles ``sync-board``: makes each named issue's board item, or every open one's, agree.

    The backfill for issues filed while nothing wrote the board, and the repair for a field
    someone changed on the board by hand or a write that failed.

    Returns:
        1 if any issue's board item could not be synced, 0 otherwise.
    """
    numbers = args.numbers or [i["number"] for i in load_issues("open", tools)]
    outcomes = tools.sync_board(numbers, tools.repo, dry_run=args.dry_run)
    counts = dict.fromkeys((UPDATED, UNCHANGED, FAILED), 0)
    for outcome, _ in outcomes.values():
        counts[outcome] += 1
    verb = "would update" if args.dry_run else "updated"
    print(
        f"{len(outcomes)} issues: {counts[UPDATED]} {verb}, "
        f"{counts[UNCHANGED]} unchanged, {counts[FAILED]} failed"
    )
    for n in sorted(outcomes):
        outcome, detail = outcomes[n]
        if outcome != UNCHANGED:
            print(f"#{n} {'failed: ' if outcome == FAILED else ''}{detail}")
    return 1 if counts[FAILED] else 0


_ISSUE_WRITES = frozenset({"edit", "close", "reopen", "create"})


def _recording_writes(tools: FindingsTools) -> tuple[FindingsTools, set[int]]:
    """``tools`` whose `gh` notes each issue a successful write changed, and that set."""
    written: set[int] = set()
    inner = tools.gh

    def gh(*argv, **kwargs):
        result = inner(*argv, **kwargs)
        if len(argv) > 2 and argv[0] == "issue" and argv[1] in _ISSUE_WRITES:
            number = created_number(
                result.stdout if argv[1] == "create" else str(argv[2])
            )
            if number is not None:
                written.add(number)
        return result

    return dataclasses.replace(tools, gh=gh), written


def cmd_list(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``list`` subcommand: prints open findings as a table or JSON.

    Open only. A closed read crossed gh's 1000-issue cap and a 120s tool timeout, so past
    findings are `history`'s search (findings_lib/history_cli.py).

    Args:
        args: parsed CLI namespace carrying ``json``, ``severity``, ``domain`` and ``limit``.
        tools: the process boundaries the issue read goes through.
    """
    rows = sorted(issue_rows(load_issues("open", tools)), key=sort_key)
    rows = [
        r
        for r in rows
        if args.severity in (None, r["severity"]) and args.domain in (None, r["domain"])
    ]
    shown = rows if args.limit is None else rows[: args.limit]
    cut = f"{len(rows) - len(shown)} more; raise --limit or drop it"
    today = today_utc()
    if args.json:
        print(json.dumps(shown))
        if len(shown) < len(rows):
            sys.stderr.write(cut + "\n")
        return 0
    print(f"{len(rows)} open: {count_line(r['severity'] or '-' for r in rows)}")
    for r in shown:
        flags = "".join(
            f" [{f}]"
            for f, on in (
                ("escalated", r["escalated"]),
                ("refuted", r["refuted"]),
                ("accepted", r["accepted"]),
                ("no-vetted-remediation", r["no_vetted_remediation"]),
                ("verify-by", r["verify_by"]),
                ("manual", r["manual"]),
                # Shown only while it still withholds: past its date the label is inert.
                (f"deferred until {r['not_before']}", is_deferred_row(r, today)),
                (f"blocked by {', '.join(r['blocked_by'])}", bool(r["blocked_by"])),
                (f"claimed:{r['claimed']}", bool(r["claimed"])),
            )
            if on
        )
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['kind'] or '-':<11} "
            f"{r['domain'] or '-':<21} since {r['first_seen']} x{r['reobservations']}{flags}  {r['title']}"
        )
    if len(shown) < len(rows):
        print(cut)
    return 0


def waiting_summary(rows: list[dict]) -> list[str]:
    """The one line `next` prints in place of ``rows``' `waiting_line`s, none for no rows."""
    if not rows:
        return []
    kinds = count_line("blocked" if r["blocked_by"] else "deferred" for r in rows)
    return [f"{len(rows)} waiting ({kinds}); --waiting to list"]


def cmd_next(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``next`` subcommand: prints the issues a session may pick up, best first.

    A git-read failure cannot be read as "no claims are live" — that would hand a stale
    guess a claimed issue to a second session. So on failure this withholds every issue that
    is CURRENTLY claimed regardless of whether the claim would otherwise read as stale,
    rather than `reap`'s outright refusal: `next` never writes, so it degrades to the more
    conservative read instead of refusing to answer at all.

    Args:
        args: parsed CLI namespace carrying ``limit``, ``json`` and ``waiting``.
        tools: the process boundaries every gh call goes through.
    """
    trees, dirty, merged, ok = tools.worktree_facts()
    issues = load_issues("open", tools)
    stale: dict[int, str] = {}
    if ok:
        states = claim_states(
            issues, trees, dirty, merged, tools.batch_holds, peer_trees=tools.peer_trees
        )
        live = {s.number for s in states if s.live}
        stale = {s.number: s.worktree for s in states if not s.live}
    else:
        sys.stderr.write(
            "warning: worktree read failed; withholding every currently claimed issue\n"
        )
        live = {i["number"] for i in issues if current_claim(i)}
    today = today_utc()
    rows = pickable(issues, live_claims=live, pr_refs=open_pr_refs(tools), today=today)
    rows = rows[: args.limit]
    text_of = {i["number"]: (i["title"], i.get("body") or "") for i in issues}
    for r in rows:
        r["solo_only"] = bool(solo_only_reasons(*text_of[r["number"]], tools.repo))
        # The holder the text render names as `[stale claim by ...]`, null when unclaimed:
        # an orchestrator reading `--json` told a free issue from one `claim` reaps (#3927).
        r["stale_claim_by"] = stale.get(r["number"])
    # Named, not hidden: withheld silently, a waiting issue reads as a closed one. Under
    # `--json` the note goes to stderr, because the array IS the free set an orchestrator
    # claims (`issue-fanout`), and a waiting row inside it would be claimed.
    held_rows = waiting(issues, today=today)
    held_back = (
        [waiting_line(r, today) for r in held_rows]
        if args.waiting
        else waiting_summary(held_rows)
    )
    if args.json:
        print(json.dumps(rows))
        for line in held_back:
            sys.stderr.write(line + "\n")
        return 0
    for r in rows:
        # A stale claim does not withhold the issue, so say who holds it and what clears it.
        # `claim` reaps it on the way past; `reap` is how the operator clears the register.
        held = (
            f"  [stale claim by `{stale[r['number']]}`]" if r["number"] in stale else ""
        )
        # Offered, not withheld: a solo session works it. Only a fan-out batch may not.
        solo = "  [solo-only: cites fan-out tooling]" if r["solo_only"] else ""
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['domain'] or '-':<21} "
            f"{r['title']}{held}{solo}"
        )
    if any(r["number"] in stale for r in rows):
        print(
            "note: a stale claim is released by `findings.py reap`; `claim` also reaps one "
            "before taking the issue"
        )
    if not rows:
        print("nothing to pick up")
    for line in held_back:
        print(line)
    return 0


# The subcommands whose verdict depends on which worktrees exist, so `--repo` needs a checkout.
_READS_WORKTREES = frozenset({"claim", "claims", "reap", "next"})


def main(argv: list[str] | None, tools: FindingsTools) -> int:
    """Entry point: parses argv and dispatches to the matching subcommand handler.

    Catches gh failures at this outer layer so every subcommand handler can call ``gh``
    directly without duplicating error handling.

    Args:
        argv: command-line arguments, or None to use ``sys.argv``.
        tools: the process boundaries. Required — the `__main__` block below is the only
            place that builds the real ones, so a caller that drops the argument gets a
            TypeError instead of silently reaching real `gh` and real subprocesses.

    Returns:
        The dispatched handler's exit code, or 1 if `gh` failed.
    """
    # The FIRST NON-BLANK line, not `[1]`. Line 1 of a module docstring is the blank line
    # after the summary, so `[1]` passed argparse an empty description and `--help` printed
    # none at all. Reading the line rather than restating it keeps one copy that cannot
    # drift.
    summary = next(line for line in __doc__.splitlines() if line.strip())
    args = _parser(summary).parse_args(argv)
    if (
        args.repo is not None
        and args.cmd in _READS_WORKTREES
        and args.repo not in REGISTER_CHECKOUTS
    ):
        # Judged against this repo's worktrees, every claim on another repo's issue names a
        # branch nothing here has checked out: `reap` would release them all and `claim`
        # would refuse every one as stale at birth.
        sys.stderr.write(
            f"{args.cmd}: --repo {args.repo} has no local checkout to judge claims against; "
            f"known: {', '.join(sorted(REGISTER_CHECKOUTS))} "
            "(findings_lib/boundaries.py REGISTER_CHECKOUTS)\n"
        )
        return FINDINGS_BAD_ARGS
    tools = aimed(tools, args.repo)
    handler = {
        "sync-labels": cmd_sync_labels,
        "sync-board": cmd_sync_board,
        "list": cmd_list,
        "history": cmd_history,
        "show": cmd_show,
        "export": cmd_export,
        "open": cmd_open,
        "touch": cmd_touch,
        "comment": cmd_comment,
        "defer": cmd_defer,
        "block": cmd_block,
        "manual": cmd_manual,
        "claim": cmd_claim,
        "release": cmd_release,
        "claims": cmd_claims,
        "reap": cmd_reap,
        "close": cmd_close,
        "verify": cmd_verify,
        "next": cmd_next,
    }[args.cmd]
    if args.cmd != "sync-board" and args.repo in (None, PR_REPO):
        tools, written = _recording_writes(tools)
    else:
        written = set()
    try:
        return handler(args, tools)
    except (subprocess.SubprocessError, OSError) as exc:
        # OSError covers a missing `gh` binary; SubprocessError covers TimeoutExpired as
        # well as the CalledProcessError whose stderr is the message worth showing.
        if isinstance(exc, subprocess.CalledProcessError):
            sys.stderr.write(
                f"gh failed ({exc.returncode}): {(exc.stderr or '').strip()}\n"
            )
        else:
            sys.stderr.write(f"gh failed: {exc}\n")
        return 1
    finally:
        if written:
            warning = board_warning(tools.sync_board(sorted(written), tools.repo))
            if warning:
                sys.stderr.write(warning + "\n")


if __name__ == "__main__":
    # The ONE site that builds the real boundaries for this module. `main` takes them as a
    # required argument so a library caller that forgets `tools` fails with a TypeError here
    # rather than reaching real `gh` and real subprocesses. `gh_calls.load_issues` keeps its
    # own default because `scripts/docs/reference/backlog.py` is a second production entry.
    raise SystemExit(main(None, FindingsTools(peer_worktrees=read_peer_worktrees)))
