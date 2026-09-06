#!/usr/bin/env python3
"""File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues.

WHY A WRAPPER. The homelab-review skill, the review-and-fix command and an ordinary session
all produce findings nobody fixes that day. Until 2026-09-02 they landed in a memory table
with no status field, a gitignored triage file that suppressed them, and session notes.
GitHub Issues has the status field; this script owns the three rules that make issues a
register rather than a pile: one issue per fingerprint, a re-observation is a comment and
the third one escalates, and a refuted finding stays closed.

Every command PLANS a list of gh argv first (pure, unit-tested), then runs it. `--dry-run`
prints the plan and writes nothing.

VERIFY-BY. `open --verify-by '<command>'` stores a read-only shell command in the issue body
under a `## Verify-by` heading, in a fenced code block so it survives a human editing the
prose around it. `verify` re-runs that command later: exit 0 means the finding is FIXED,
non-zero means it still reproduces. It refuses to run anything the repo's own read-only
classifier (`.claude/hooks/auto-approve-readonly.py`, the same judgment that decides what a
session can run without a prompt) does not clear — a command stored by `open` but never
validated there is still only ever run through that gate.

This file is the CLI: the `cmd_*` handlers and the exit contract. Argument parsing is
`findings_cli.py`, the vocabulary and the pure reads are `findings_model.py`, the gh argv are
`findings_plans.py`, the gh calls are `findings_gh.py`, claim staleness is `findings_claim.py`,
the four claim subcommands are `findings_claim_cli.py`, and verify-by is `findings_verify.py`.

Usage::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] \\
        [--verify-by 'uv run python scripts/diagnostics/probe.py health <svc>'] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py claim 688 701 --worktree worktree-foo \\
        [--session id] [--force]
    uv run python scripts/dev/findings.py release 688 --worktree worktree-foo [--reason "..."]
    uv run python scripts/dev/findings.py claims [--json]
    uv run python scripts/dev/findings.py reap [--dry-run]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py close 688 --accepted --reason "..."
    uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]
    uv run python scripts/dev/findings.py verify --all [--close] [--close-claimed] \\
        [--timeout 120]
    uv run python scripts/dev/findings.py verify 688 701 [--close]
    uv run python scripts/dev/findings.py next [--limit N] [--json]

CLOSING A FINDING. `--fixed` closes as completed. The other two close as not planned and are
terminal, so `open` refuses to re-file the same fingerprint afterwards: `--refuted` records
that a skeptic disproved it, and `--accepted` records that it is TRUE and the operator chose
to live with the trade-off. Both need `--reason`. Reach for `--accepted` rather than closing
by hand — a hand-close is invisible to the dedup, so the next review re-files an accepted
decision and the comment on it reads "treat as a regression".

RELEASING A STRANDED CLAIM. Every path that ends or restarts an issue's life releases the
claim on it first, whoever holds it — `close`, `verify --close`, and `open`'s reopen path
alike, all through `_release_held_claim`. `claims`, `reap` and `next` read OPEN issues, so a
claim left on a closed one is invisible to every view at once rather than merely wrong, and a
reopen brings it back LIVE (#1277). `verify --close` releases only a claim it is allowed to
close over: a LIVE claim withholds the close entirely rather than being released under the
session working it, and `--close-claimed` is the way past that (#1302).

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
withholding `manual` issues, issues a LIVE claim already holds, and issues an open PR already
says it closes. An issue whose claim is stale IS offered, marked with who holds it — `claim`
reaps that claim on the way past, and `reap` clears every one of them at once. `--limit N`
bounds the list; there is no default bound, because one truncated the free set silently and
the reader took ten rows for all of them.

Exit codes: 0 done; 1 gh failed, or `reap` refused a git read failure rather than call it
"nothing is claimed"; 2 bad arguments, which includes a `--worktree` name the claim trailer
could not carry; 3 nothing was written because the issue refuses it — closed, `manual`,
outside the register, held by another worktree, not claimed, or lost a race to another
claim — or because `claim`'s own `--worktree` would read stale at birth, or because
`verify --close` withheld a close a live claim holds.
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

# DECIDED: the leaves are imported as `dev.<leaf>`, never as bare siblings.
# `scripts/docs/reference/backlog.py` reaches this code as `dev.findings` with only
# `scripts/` on sys.path, so a bare `from findings_model import ...` would raise
# ModuleNotFoundError under the docs-refresh cron while pytest stayed green.
from dev.findings_claim import claim_states
from dev.findings_claim_cli import cmd_claim, cmd_claims, cmd_reap, cmd_release
from dev.findings_cli import _parser
from dev.findings_gh import (
    _create_with_optional_project,
    _existing_labels,
    _load_issue,
    load_issues,
    open_pr_refs,
    run,
)
from dev.findings_model import (
    NO_REOPEN,
    current_claim,
    now_iso,
    find_by_fingerprint,
    fingerprint,
    issue_rows,
    label_names,
    pickable,
    sort_key,
)
from dev.findings_plans import (
    plan_close,
    plan_open,
    plan_release,
    plan_sync_labels,
    plan_touch,
)
from dev.findings_tools import FindingsTools
from dev.findings_verify import (
    verify_close_comment,
    verify_finding,
)


def _release_held_claim(issue: dict, reason: str) -> list[list[str]]:
    """The gh argv releasing whatever claim ``issue`` carries, or ``[]`` when it carries none.

    Releases whoever holds it, not just the caller's own worktree. `claims`, `reap` and
    `next` all read OPEN issues, so a claim left on a closed one is invisible to every view
    at once — wrong rather than merely stale, and unreapable.

    Hoisted out of `cmd_close` so `cmd_verify`'s `--close` loop and `cmd_open`'s reopen path
    release the same way (#1277). `verify --close` called `plan_close` directly and stranded
    every claim it closed; a `Closes #<n>` merge strands one too, and `plan_open` reopening
    that issue for a later re-observation brought the stale claim back LIVE, blocking `claim`
    and withholding the issue from `next` for as long as the claiming worktree existed.
    """
    held = current_claim(issue)
    if not held:
        return []
    return plan_release(issue, worktree=held, when=now_iso(), reason=reason)


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
    # `gh issue create --label` fails on a label the repo does not have, so the first `open`
    # in a fresh repo has to create the label set before it can use it.
    run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    existing = find_by_fingerprint(load_issues("all", tools), fp)
    outcome, code, plans = plan_open(
        existing,
        title=args.title,
        body=body,
        labels=labels,
        fp=fp,
        source=args.source,
        verify_by=args.verify_by,
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
        # claiming worktree exists — an orchestrator's can be a long time (#1277). Released
        # as its OWN comment rather than folded into the regression note, so the body never
        # carries two claim trailers at once (see `current_claim`'s DECIDED marker).
        plans += _release_held_claim(existing, "reopened after a re-observation")
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
    plans = _release_held_claim(issue, f"closed as {outcome}")
    plans += plan_close(args.number, outcome=outcome, pr=args.pr, reason=args.reason)
    run(plans, args.dry_run, tools)
    print(f"#{args.number} closed as {outcome}")
    return 0


def cmd_verify(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``verify`` subcommand: re-runs each finding's stored verify-by command.

    ``--dry-run`` only gates the gh writes a passing ``--close`` would make; the verify-by
    commands themselves always run — producing a verdict requires it, and they were already
    proven read-only by `classify_verify_command` before they run at all.

    AN `error` IS NOT A REPRODUCTION (#1308). `run_verify_by` already separates a predicate
    that RAN and failed (``still-open``) from one that never produced a verdict at all
    (``error``: refused by the classifier, timed out, or could not be launched), and it
    carries the reason. That reason used to be computed and dropped, so a predicate nothing
    could ever run read as a finding that keeps reproducing. Each `error` row now prints its
    reason, and a run with any of them ends with a count. The exit code stays 0: it answers
    "did verify run", not "what did verify find", and every caller reads it that way.
    A predicate that runs fine but can never pass — the unsatisfiable anchor in #1308's own
    example — still reads ``still-open`` and is NOT covered here.

    A LIVE CLAIM WITHHOLDS THE CLOSE (#1302). Every other write in the protocol refuses on a
    live claim — `plan_claim` raises, `plan_release` refuses a claim but its own, `cmd_claim`
    reaps only a claim it has proved stale, `cmd_next` withholds. `verify --close` was the one
    write that read no claim before acting, so an unrelated verify run could close an issue
    out from under the session working it; releasing the claim first (#1277) made the takeover
    tidy rather than preventing it. A FAILED worktree read withholds the close too, matching
    `cmd_next`'s conservative degradation rather than `cmd_reap`'s outright refusal.
    ``--close-claimed`` is the way past both, and skips the git read entirely. So does an
    ordinary run closing unclaimed findings: `current_claim` is a pure read of comments
    already in hand, so nothing pays for `worktree_facts` unless a claim really sits on an
    issue the run is about to close.

    Args:
        args: parsed CLI namespace carrying ``all``, ``numbers``, ``close``,
            ``close_claimed``, ``timeout`` and ``dry_run``.
        tools: the process boundaries every gh call and every verify-by command goes through.

    Returns:
        2 if neither or both of ``--all``/issue numbers were given, 3 if a close was
        withheld because a live claim holds the issue, 0 otherwise.
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
    results = [
        (
            issue,
            issue["number"],
            issue["title"],
            *verify_finding(issue, args.timeout, tools),
        )
        for issue in issues
    ]
    for _issue, number, title, verdict, detail, _command in results:
        print(f"#{number:<5} {verdict:<11} {title}")
        # The `error` reason is the whole of what distinguishes "the predicate never ran"
        # from "the finding still reproduces", and printing the verdict word alone threw it
        # away (#1308). Refused / timed out / could not launch are three different operator
        # actions, so the reason is printed rather than a generic marker.
        if verdict == "error":
            print(f"       └─ {detail.strip() or 'no reason given'}")
    errored = sum(1 for r in results if r[3] == "error")
    if errored:
        print(
            f"verify: {errored} predicate(s) never ran — an `error` is not a reproduction; "
            "fix the predicate before reading the finding as unfixed"
        )
    if not args.close:
        return 0
    withheld = False
    live: set[int] = set()
    # `current_claim` is a pure read of comments already in hand, so the expensive git read
    # below is paid only when a close is actually on the table AND something holds one of
    # those issues. A plain `verify`, a run that found nothing fixed, and the ordinary case
    # of closing an unclaimed finding all cost no git at all.
    claimed_fixed = [r[0] for r in results if r[3] == "fixed" and current_claim(r[0])]
    if claimed_fixed and not args.close_claimed:
        # Read under --dry-run too: it is not a gh write, and a dry run must show the same
        # skip decisions the real run would make.
        trees, dirty, merged, ok = tools.worktree_facts()
        if ok:
            live = {
                s.number
                for s in claim_states(claimed_fixed, trees, dirty, merged)
                if s.live
            }
        else:
            sys.stderr.write(
                "warning: worktree read failed; withholding the close on every claimed "
                "issue (pass --close-claimed to close anyway)\n"
            )
            live = {i["number"] for i in claimed_fixed}
    for issue, number, _title, verdict, detail, command in results:
        if verdict != "fixed":
            continue
        if number in live:
            print(
                f"#{number} not closed: `{current_claim(issue)}` holds a live claim; "
                "pass --close-claimed to close it anyway"
            )
            withheld = True
            continue
        comment = verify_close_comment(command, detail)
        # Same release `cmd_close` makes, for the same reason: this path called
        # `plan_close` directly and stranded the claim on every issue it closed (#1277).
        plans = _release_held_claim(issue, "closed as fixed by verify-by")
        plans += plan_close(number, outcome="fixed", comment=comment)
        run(plans, args.dry_run, tools)
        print(f"#{number} closed as fixed (verify-by)")
    return 3 if withheld else 0


def cmd_sync_labels(args: argparse.Namespace, tools: FindingsTools) -> int:
    plans = plan_sync_labels(_existing_labels(tools))
    run(plans, args.dry_run, tools)
    print(f"sync-labels: {len(plans)} label(s) created")
    return 0


def cmd_list(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``list`` subcommand: prints open findings as a table or JSON.

    Args:
        args: parsed CLI namespace carrying ``state`` and ``json``.
        tools: the process boundaries the issue read goes through.
    """
    rows = sorted(issue_rows(load_issues(args.state, tools)), key=sort_key)
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
    rows = pickable(issues, live_claims=live, pr_refs=open_pr_refs(tools))[: args.limit]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        # A stale claim does not withhold the issue, so say who holds it and what clears it.
        # `claim` reaps it on the way past; `reap` is how the operator clears the register.
        held = (
            f"  [stale claim by `{stale[r['number']]}`]" if r["number"] in stale else ""
        )
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['domain'] or '-':<21} "
            f"{r['title']}{held}"
        )
    if any(r["number"] in stale for r in rows):
        print(
            "note: a stale claim is released by `findings.py reap`; `claim` also reaps one "
            "before taking the issue"
        )
    if not rows:
        print("nothing to pick up")
    return 0


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
    # none at all (#1272). Reading the line rather than restating it keeps one copy: an
    # earlier round replaced the broken expression with a hardcoded paraphrase, which a
    # reviewer rejected as a second copy that can drift.
    summary = next(line for line in __doc__.splitlines() if line.strip())
    args = _parser(summary).parse_args(argv)
    handler = {
        "sync-labels": cmd_sync_labels,
        "list": cmd_list,
        "open": cmd_open,
        "touch": cmd_touch,
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
    # rather than reaching real `gh` and real subprocesses. `findings_gh.load_issues` keeps its
    # own default because `scripts/docs/reference/backlog.py` is a second production entry.
    raise SystemExit(main(None, FindingsTools()))
