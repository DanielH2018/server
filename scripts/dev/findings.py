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
and verify-by is `findings_verify.py`.

Usage::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] \\
        [--verify-by 'uv run python scripts/diagnostics/probe.py health <svc>'] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py claim 688 701 --worktree worktree-foo [--session id]
    uv run python scripts/dev/findings.py release 688 --worktree worktree-foo [--reason "..."]
    uv run python scripts/dev/findings.py claims [--json]
    uv run python scripts/dev/findings.py reap [--dry-run]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py close 688 --accepted --reason "..."
    uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]
    uv run python scripts/dev/findings.py verify --all [--close] [--timeout 120]
    uv run python scripts/dev/findings.py verify 688 701 [--close]
    uv run python scripts/dev/findings.py next [--limit 10] [--json]

CLOSING A FINDING. `--fixed` closes as completed. The other two close as not planned and are
terminal, so `open` refuses to re-file the same fingerprint afterwards: `--refuted` records
that a skeptic disproved it, and `--accepted` records that it is TRUE and the operator chose
to live with the trade-off. Both need `--reason`. Reach for `--accepted` rather than closing
by hand — a hand-close is invisible to the dedup, so the next review re-files an accepted
decision and the comment on it reads "treat as a regression". Closing releases any claim it
finds first, whoever holds it — not just the caller's own worktree — so `claims`, `reap` and
`next` all stop showing the issue at once rather than leaving a claim stranded on a closed row.

CLAIMING AN ISSUE. `claim` posts a `Claim:` comment and adds the `claimed` label, so a
worktree fanning out several issues at once knows which are its own; `release` reverses
that. Both refuse an issue another worktree already holds, `claim` also refuses `manual` and
closed issues, and re-claiming an issue your own worktree already holds is a no-op. `claims`
lists every claim, warning on stderr (but still rendering) if the worktree read failed;
`reap` refuses outright on that same failure. `claim` takes every issue it can and refuses
the rest, so one `manual` issue does not cost the good claims in the same batch.

PICKING UP WORK. `next` prints the issues a session may claim, best severity first,
withholding `manual` issues, issues a live claim already holds, and issues an open PR already
says it closes.

Exit codes: 0 done; 1 gh failed, or `reap` refused a git read failure rather than call it
"nothing is claimed"; 2 bad arguments; 3 nothing was written because the issue refuses it —
closed, `manual`, held by another worktree, not claimed, or lost a race to another claim.
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime

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
from dev.findings_cli import _parser
from dev.findings_gh import (
    _create_with_optional_project,
    _existing_labels,
    _load_issue,
    load_issues,
    run,
)
from dev.findings_model import (
    NO_REOPEN,
    current_claim,
    find_by_fingerprint,
    fingerprint,
    issue_rows,
    label_names,
    pr_refs,
    sort_key,
)
from dev.findings_plans import (
    ClaimRefused,
    plan_claim,
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
from dev.prune_worktrees import _worktree_facts


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


def cmd_claim(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Claims one or more issues for a worktree.

    Takes every issue it can and refuses the rest, rather than refusing the whole batch on
    one bad issue: a fan-out claims several issues at once, and losing four good claims
    because the fifth is `manual` would mean re-running and re-reading everything.

    Returns 0 when every issue was taken, 3 when any was refused.
    """
    # `gh issue edit --add-label` fails on a label the repo lacks, which is why cmd_close
    # syncs first. Without this the FIRST claim posts its comment, fails the label edit and
    # exits 1; the retry then reads "already claimed" and the label is never added at all.
    run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    refused = False
    for number in args.numbers:
        issue = _load_issue(number, tools)
        try:
            plans = plan_claim(
                issue, worktree=args.worktree, session=args.session, when=_now()
            )
        except ClaimRefused as exc:
            print(f"#{number} refused: {exc.reason}")
            refused = True
            continue
        if not plans:
            print(f"#{number} already claimed by `{args.worktree}`")
            continue
        run(plans, args.dry_run, tools)
        if args.dry_run:
            print(f"#{number} would be claimed by `{args.worktree}`")
            continue
        # Read back. Two sessions can both post a claim comment; the FIRST one holds it,
        # and without this the loser prints success and starts work on someone else's issue.
        winner = current_claim(_load_issue(number, tools))
        if winner != args.worktree:
            print(f"#{number} lost the race to `{winner}`")
            refused = True
            continue
        print(f"#{number} claimed by `{args.worktree}`")
    return 3 if refused else 0


def cmd_release(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Releases this worktree's claim on one or more issues."""
    refused = False
    for number in args.numbers:
        issue = _load_issue(number, tools)
        try:
            plans = plan_release(
                issue, worktree=args.worktree, when=_now(), reason=args.reason
            )
        except ClaimRefused as exc:
            print(f"#{number} refused: {exc.reason}")
            refused = True
            continue
        run(plans, args.dry_run, tools)
        verb = "would be released" if args.dry_run else "released"
        print(f"#{number} {verb} by `{args.worktree}`")
    return 3 if refused else 0


def cmd_claims(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Prints every open claim: issue, worktree, live or stale, and why."""
    trees, dirty, merged, ok = _worktree_facts()
    if not ok:
        # `reap` refuses instead; a read can render, but must say staleness is a guess.
        sys.stderr.write("warning: worktree read failed; STALE below is unverified\n")
    states = claim_states(load_issues("open", tools), trees, dirty, merged)
    if args.json:
        print(json.dumps([vars(s) for s in states], indent=2))
        return 0
    for s in states:
        age = f"{s.age_days}d" if s.age_days is not None else "?"
        print(
            f"#{s.number:<5} {'live ' if s.live else 'STALE'} {age:>4} "
            f"{s.worktree:<40} {s.reason}"
        )
    if not states:
        print("no open claims")
    return 0


def cmd_reap(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Releases every stale claim, printing why each was judged stale."""
    trees, dirty, merged, ok = _worktree_facts()
    if not ok:
        # Must not read a git failure as "every worktree is gone" and release everything.
        print("reap: could not read git; refusing to release anything")
        return 1
    issues = load_issues("open", tools)
    by_number = {i["number"]: i for i in issues}
    reaped = 0
    for s in claim_states(issues, trees, dirty, merged):
        if s.live:
            continue
        plans = plan_release(
            by_number[s.number],
            worktree=s.worktree,
            when=_now(),
            reason=f"reaped: {s.reason}",
        )
        run(plans, args.dry_run, tools)
        verb = "would be released" if args.dry_run else "released"
        print(f"#{s.number} {verb} — {s.reason}")
        reaped += 1
    verb = "would be released" if args.dry_run else "released"
    print(f"reap: {reaped} stale claim(s) {verb}")
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
    held = current_claim(issue)
    plans = []
    if held:
        plans += plan_release(
            issue, worktree=held, when=_now(), reason=f"closed as {outcome}"
        )
    plans += plan_close(args.number, outcome=outcome, pr=args.pr, reason=args.reason)
    run(plans, args.dry_run, tools)
    print(f"#{args.number} closed as {outcome}")
    return 0


def cmd_verify(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``verify`` subcommand: re-runs each finding's stored verify-by command.

    ``--dry-run`` only gates the gh writes a passing ``--close`` would make; the verify-by
    commands themselves always run — producing a verdict requires it, and they were already
    proven read-only by `classify_verify_command` before they run at all.

    Args:
        args: parsed CLI namespace carrying ``all``, ``numbers``, ``close``, ``timeout`` and
            ``dry_run``.
        tools: the process boundaries every gh call and every verify-by command goes through.

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
    results = [
        (issue["number"], issue["title"], *verify_finding(issue, args.timeout, tools))
        for issue in issues
    ]
    for number, title, verdict, _detail, _command in results:
        print(f"#{number:<5} {verdict:<11} {title}")
    if args.close:
        for number, _title, verdict, detail, command in results:
            if verdict != "fixed":
                continue
            comment = verify_close_comment(command, detail)
            run(
                plan_close(number, outcome="fixed", comment=comment),
                args.dry_run,
                tools,
            )
            print(f"#{number} closed as fixed (verify-by)")
    return 0


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


def pickable(
    issues: list[dict], *, live_claims: set[int], pr_refs: set[int]
) -> list[dict]:
    """The issues a session may pick up, best first.

    Args:
        live_claims: issue numbers whose claim is still live. A STALE claim does not
            withhold an issue — that is the whole point of `reap`.
        pr_refs: issue numbers an open PR already says it closes. Without this, a session
            picks up work another session has finished but not yet landed.
    """
    rows = [
        r
        for r in issue_rows(issues)
        if not r["manual"]
        and r["number"] not in live_claims
        and r["number"] not in pr_refs
    ]
    return sorted(rows, key=sort_key)


def _open_pr_refs(tools: FindingsTools) -> set[int]:
    """Issue numbers the open PRs say they close."""
    prs = tools.gh_json(
        "pr", "list", "--state", "open", "--limit", "200", "--json", "body"
    )
    return pr_refs([pr.get("body") or "" for pr in prs or []])


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
    trees, dirty, merged, ok = _worktree_facts()
    issues = load_issues("open", tools)
    if ok:
        live = {s.number for s in claim_states(issues, trees, dirty, merged) if s.live}
    else:
        sys.stderr.write(
            "warning: worktree read failed; withholding every currently claimed issue\n"
        )
        live = {i["number"] for i in issues if current_claim(i)}
    rows = pickable(issues, live_claims=live, pr_refs=_open_pr_refs(tools))[
        : args.limit
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['domain'] or '-':<21} {r['title']}"
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
    args = _parser(__doc__.splitlines()[1]).parse_args(argv)
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
