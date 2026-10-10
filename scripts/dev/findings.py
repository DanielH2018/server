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
    uv run python scripts/dev/findings.py defer 688 --until 2026-09-12
    uv run python scripts/dev/findings.py defer 688 --clear
    uv run python scripts/dev/findings.py manual 688 [--clear]
    uv run python scripts/dev/findings.py claim 688 701 --worktree worktree-foo \\
        [--session id] [--force]
    uv run python scripts/dev/findings.py release 688 --worktree worktree-foo [--reason "..."]
    uv run python scripts/dev/findings.py claims [--worktree <branch>] [--json]
    uv run python scripts/dev/findings.py reap [--dry-run]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py close 688 --accepted --reason "..."
    uv run python scripts/dev/findings.py list [--json]
    uv run python scripts/dev/findings.py history <terms> [--file path] [--limit N] [--json]
    uv run python scripts/dev/findings.py show 688 [--json]
    uv run python scripts/dev/findings.py export --out register.json
    uv run python scripts/dev/findings.py verify --all
    uv run python scripts/dev/findings.py verify 688 701
    uv run python scripts/dev/findings.py next [--limit N] [--json]

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
withholding `manual` issues, issues deferred to a later date, issues a LIVE claim already
holds, and issues an open PR already says it closes. An issue whose claim is stale IS
offered, marked with who holds it — `claim` reaps that claim on the way past, and `reap`
clears every one of them at once. `--limit N` bounds the list; there is no default bound,
because one truncated the free set silently and the reader took ten rows for all of them.

DEFERRING AN ISSUE. `defer <n> --until <date>` (or `open --not-before <date>`) puts a `not-before:<YYYY-MM-DD>`
label on the issue. `next` and `claim` withhold it while today is before that date and offer it ON the date, with
nothing to clear: the label expires by comparison, which `manual` cannot. `next` still names a deferred issue —
under a `deferred:` line, and on stderr under `--json` so the array stays the free set — because a silently
withheld issue reads as a closed one to whoever looks at the backlog. `defer <n> --clear` lifts it early.

RESERVING AN ISSUE FOR THE OPERATOR. `manual <n>` (or `open --manual`) adds the `manual`
label: `next` withholds the issue, `claim` refuses it, and `list` still marks it `[manual]`.
Unlike a date, it never expires; `manual <n> --clear` removes it. Marking an issue manual also
releases any claim on it, because `reap` skips `manual` issues and would never clear that
claim. `open --manual` labels an issue the dedup matched as well as a new one, so the flag is
never dropped silently.

Exit codes: 0 done; 1 gh failed, or `reap` refused a git read failure rather than call it
"nothing is claimed"; 2 bad arguments, which includes a `--worktree` name the claim trailer
could not carry; 3 nothing was written because the issue refuses it — closed, `manual`,
deferred to a later date, outside the register, held by another worktree, not claimed, or
lost a race to another claim — or because `claim`'s own `--worktree` would read stale at
birth, or because `manual` found the label already in the state it was asked for.
"""

import argparse
import json
import subprocess
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# DECIDED: the leaves are imported as `dev.findings_lib.<leaf>`, never as bare siblings.
# `scripts/docs/reference/backlog.py` reaches this code as `dev.findings` with only
# `scripts/` on sys.path, so a bare `from issue_model import ...` would raise
# ModuleNotFoundError under the docs-refresh cron while pytest stayed green.
from dev.findings_lib.claim import claim_states
from dev.findings_lib.claim_cli import cmd_claim, cmd_claims, cmd_reap, cmd_release
from dev.findings_lib.cli import _parser
from dev.findings_lib.export_cli import cmd_export
from dev.findings_lib.history_cli import cmd_history, cmd_show
from dev.findings_lib.gh_calls import (
    _create_with_optional_project,
    _existing_labels,
    _load_issue,
    fingerprint_match,
    load_issues,
    open_pr_refs,
    run,
)
from dev.findings_lib.issue_model import (
    NO_REOPEN,
    cited_paths,
    current_claim,
    deferred,
    now_iso,
    fingerprint,
    issue_rows,
    label_names,
    not_before_label,
    pickable,
    sort_key,
    today_utc,
)
from dev.findings_lib.plans import (
    ClaimRefused,
    plan_close,
    plan_defer,
    plan_ensure_label,
    plan_manual,
    plan_release_held,
    plan_open,
    plan_sync_labels,
    plan_touch,
)
from dev.findings_lib.boundaries import REGISTER_CHECKOUTS, FindingsTools, aimed
from dev.findings_lib.red_green import RED_GREEN_LABEL, red_green_eligible
from dev.findings_lib.solo_only import fanout_tooling_paths
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
        The process exit code: 0 on success, 2 if the body file is missing, 3 if the
        fingerprint belongs to an issue closed as refuted or accepted.
    """
    if not args.body_file.is_file():
        sys.stderr.write(f"open: body file not found: {args.body_file}\n")
        return 2
    body = args.body_file.read_text()
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
            print(f"(dry-run) would create; fingerprint {fp}")
            return 0
        url = _create_with_optional_project(plans[0], tools)
        print(f"#{url.rsplit('/', 1)[-1]} created  {url}")
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
        print(f"#{args.number} is closed ({why}); use open to re-file")
        return 3
    plans = plan_touch(issue, args.source)
    run(plans, args.dry_run, tools)
    escalated = any(p[:2] == ["issue", "edit"] for p in plans)
    print(f"#{args.number} touched{' and escalated' if escalated else ''}")
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
        print(f"#{args.number} refused: {exc.reason}")
        return 3
    run(plans, args.dry_run, tools)
    what = (
        "deferral cleared" if args.clear else f"deferred until {args.until.isoformat()}"
    )
    print(f"#{args.number} {what}")
    return 0


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
        print(f"#{args.number} refused: {exc.reason}")
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
        return 2
    if outcome != "fixed" and not args.reason:
        sys.stderr.write(
            f"close --{outcome} needs --reason: a bare verdict teaches the next run nothing\n"
        )
        return 2
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
        2 if neither or both of ``--all``/issue numbers were given, 0 otherwise.
    """
    if args.all and args.numbers:
        sys.stderr.write("verify: pass --all or issue numbers, not both\n")
        return 2
    if not args.all and not args.numbers:
        sys.stderr.write("verify: need --all or at least one issue number\n")
        return 2
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


def _still_deferred(row: dict, today) -> bool:
    return bool(row["not_before"]) and today.isoformat() < row["not_before"]


def cmd_list(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``list`` subcommand: prints open findings as a table or JSON.

    Open only. A closed read crossed gh's 1000-issue cap and a 120s tool timeout, so past
    findings are `history`'s search (findings_lib/history_cli.py).

    Args:
        args: parsed CLI namespace carrying ``json``.
        tools: the process boundaries the issue read goes through.
    """
    rows = sorted(issue_rows(load_issues("open", tools)), key=sort_key)
    today = today_utc()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
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
                (f"deferred until {r['not_before']}", _still_deferred(r, today)),
                (f"claimed:{r['claimed']}", bool(r["claimed"])),
            )
            if on
        )
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['kind'] or '-':<11} "
            f"{r['domain'] or '-':<21} since {r['first_seen']} x{r['reobservations']}{flags}  {r['title']}"
        )
    return 0


def cmd_next(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``next`` subcommand: prints the issues a session may pick up, best first.

    A git-read failure cannot be read as "no claims are live" — that would hand a stale
    guess a claimed issue to a second session. So on failure this withholds every issue that
    is CURRENTLY claimed regardless of whether the claim would otherwise read as stale,
    rather than `reap`'s outright refusal: `next` never writes, so it degrades to the more
    conservative read instead of refusing to answer at all.

    Args:
        args: parsed CLI namespace carrying ``limit`` and ``json``.
        tools: the process boundaries every gh call goes through.
    """
    trees, dirty, merged, ok = tools.worktree_facts()
    issues = load_issues("open", tools)
    stale: dict[int, str] = {}
    if ok:
        states = claim_states(issues, trees, dirty, merged)
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
    bodies = {i["number"]: i.get("body") or "" for i in issues}
    for r in rows:
        r["solo_only"] = bool(fanout_tooling_paths(bodies[r["number"]]))
        # The holder the text render names as `[stale claim by ...]`, null when unclaimed:
        # an orchestrator reading `--json` told a free issue from one `claim` reaps (#3927).
        r["stale_claim_by"] = stale.get(r["number"])
    # Named, not hidden: withheld silently, a deferred issue reads as a closed one. Under
    # `--json` the note goes to stderr, because the array IS the free set an orchestrator
    # claims (`issue-fanout`), and a deferred row inside it would be claimed.
    held_back = [
        f"deferred: #{r['number']} until {r['not_before']}  {r['title']}"
        for r in deferred(issues, today=today)
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
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
        return 2
    tools = aimed(tools, args.repo)
    handler = {
        "sync-labels": cmd_sync_labels,
        "list": cmd_list,
        "history": cmd_history,
        "show": cmd_show,
        "export": cmd_export,
        "open": cmd_open,
        "touch": cmd_touch,
        "defer": cmd_defer,
        "manual": cmd_manual,
        "claim": cmd_claim,
        "release": cmd_release,
        "claims": cmd_claims,
        "reap": cmd_reap,
        "close": cmd_close,
        "verify": cmd_verify,
        "next": cmd_next,
    }[args.cmd]
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


if __name__ == "__main__":
    # The ONE site that builds the real boundaries for this module. `main` takes them as a
    # required argument so a library caller that forgets `tools` fails with a TypeError here
    # rather than reaching real `gh` and real subprocesses. `gh_calls.load_issues` keeps its
    # own default because `scripts/docs/reference/backlog.py` is a second production entry.
    raise SystemExit(main(None, FindingsTools()))
