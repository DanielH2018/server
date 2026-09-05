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

This file is the CLI: argument parsing, the six `cmd_*` handlers and the exit contract. The
vocabulary and the pure reads are `findings_model.py`, the gh argv every command plans are
`findings_plans.py`, the gh calls are `findings_gh.py`, and the verify-by gate is
`findings_verify.py`.

Usage::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] \\
        [--verify-by 'uv run python scripts/diagnostics/probe.py health <svc>'] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]
    uv run python scripts/dev/findings.py verify --all [--close] [--timeout 120]
    uv run python scripts/dev/findings.py verify 688 701 [--close]

Exit codes: 0 done; 1 gh failed (its stderr is printed); 2 bad arguments;
3 nothing was written because the issue refuses it — the fingerprint belongs to an issue
closed as refuted, or `touch` was given a closed issue.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# DECIDED: the leaves are imported as `dev.<leaf>`, never as bare siblings.
# `scripts/docs/reference/backlog.py` reaches this code as `dev.findings` with only
# `scripts/` on sys.path, so a bare `from findings_model import ...` would raise
# ModuleNotFoundError under the docs-refresh cron while pytest stayed green.
from dev.findings_gh import (
    _create_with_optional_project,
    _existing_labels,
    _load_issue,
    load_issues,
    run,
)
from dev.findings_model import (
    DOMAINS,
    KINDS,
    SEVERITIES,
    find_by_fingerprint,
    fingerprint,
    issue_rows,
    label_names,
    sort_key,
)
from dev.findings_plans import plan_close, plan_open, plan_sync_labels, plan_touch
from dev.findings_tools import FindingsTools
from dev.findings_verify import (
    DEFAULT_VERIFY_TIMEOUT,
    verify_close_comment,
    verify_finding,
)


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
        fingerprint belongs to an issue closed as refuted.
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
    if outcome == "refuted":
        print(
            f"#{existing['number']} refuted: closed on {(existing.get('closedAt') or '?')[:10]}; not reopened"
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
        why = "refuted" if "refuted" in label_names(issue) else "fixed"
        print(f"#{args.number} is closed ({why}); use open to re-file")
        return 3
    plans = plan_touch(issue, args.source)
    run(plans, args.dry_run, tools)
    escalated = any(p[:2] == ["issue", "edit"] for p in plans)
    print(f"#{args.number} touched{' and escalated' if escalated else ''}")
    return 0


def cmd_close(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``close`` subcommand: closes an issue as fixed or refuted.

    Args:
        args: parsed CLI namespace for the ``close`` subcommand.
        tools: the process boundaries every gh call goes through.

    Returns:
        2 if ``--refuted`` is combined with ``--pr`` or missing ``--reason``, 0 otherwise.
    """
    # argparse cannot express "--pr only with --fixed" across a mutually exclusive group.
    if args.refuted and args.pr:
        sys.stderr.write("close --pr goes with --fixed\n")
        return 2
    if args.refuted and not args.reason:
        sys.stderr.write(
            "close --refuted needs --reason: a bare refutation teaches the next run nothing\n"
        )
        return 2
    run(
        plan_close(args.number, fixed=args.fixed, pr=args.pr, reason=args.reason),
        args.dry_run,
        tools,
    )
    print(f"#{args.number} closed as {'fixed' if args.fixed else 'refuted'}")
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
                plan_close(number, fixed=True, pr=None, reason=None, comment=comment),
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
                ("no-vetted-remediation", r["no_vetted_remediation"]),
                ("verify-by", r["verify_by"]),
            )
            if on
        )
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['kind'] or '-':<11} "
            f"{r['domain'] or '-':<21} since {r['first_seen']} x{r['reobservations']}{flags}  {r['title']}"
        )
    return 0


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add ``--dry-run`` to ``parser``.

    Every subparser gets its own copy so the flag parses on either side of the
    subcommand name — argparse only accepts a parent-parser optional before the
    subcommand token. ``suppress=True`` (used on the subparsers) sets
    ``default=argparse.SUPPRESS`` so an absent subparser flag leaves the top-level
    parser's own default in place instead of overwriting it back to ``False``.
    """
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="print the gh commands, write nothing",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    _add_dry_run(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser(
        "open", help="file a finding, or touch/reopen the existing issue"
    )
    _add_dry_run(o, suppress=True)
    o.add_argument("--title", required=True)
    o.add_argument("--body-file", required=True, type=Path)
    o.add_argument("--severity", required=True, choices=SEVERITIES)
    o.add_argument("--kind", required=True, choices=KINDS)
    o.add_argument("--domain", choices=DOMAINS)
    o.add_argument(
        "--file",
        help="primary file:line the finding cites; the line is dropped from the fingerprint",
    )
    o.add_argument("--source", default="session", help="review-<date> or session")
    o.add_argument("--no-vetted-remediation", action="store_true")
    o.add_argument(
        "--verify-by",
        help="read-only command; exit 0 means fixed, non-zero means it still reproduces",
    )

    t = sub.add_parser(
        "touch", help="record a re-observation; the third adds escalated"
    )
    _add_dry_run(t, suppress=True)
    t.add_argument("number", type=int)
    t.add_argument("--source", default="session")

    c = sub.add_parser("close", help="close as fixed or refuted")
    _add_dry_run(c, suppress=True)
    c.add_argument("number", type=int)
    how = c.add_mutually_exclusive_group(required=True)
    how.add_argument("--fixed", action="store_true")
    how.add_argument("--refuted", action="store_true")
    c.add_argument("--pr", type=int, help="the PR that fixed it")
    c.add_argument("--reason", help="required with --refuted: what disproved it")

    ls = sub.add_parser("list", help="rows for the review skill and the docs generator")
    _add_dry_run(ls, suppress=True)
    ls.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ls.add_argument("--json", action="store_true")

    sl = sub.add_parser("sync-labels", help="create any missing label")
    _add_dry_run(sl, suppress=True)

    v = sub.add_parser(
        "verify",
        help="re-run each finding's verify-by command and report fixed/still-open",
    )
    _add_dry_run(v, suppress=True)
    v.add_argument("numbers", nargs="*", type=int, help="issue numbers to verify")
    v.add_argument("--all", action="store_true", help="verify every open finding")
    v.add_argument(
        "--close", action="store_true", help="close passing findings as fixed"
    )
    v.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        help="seconds before a verify-by command counts as an error",
    )
    return p


def main(argv: list[str] | None = None, tools: FindingsTools | None = None) -> int:
    """Entry point: parses argv and dispatches to the matching subcommand handler.

    Catches gh failures at this outer layer so every subcommand handler can call ``gh``
    directly without duplicating error handling.

    Args:
        argv: command-line arguments, or None to use ``sys.argv``.
        tools: the process boundaries; the real ones when omitted.

    Returns:
        The dispatched handler's exit code, or 1 if `gh` failed.
    """
    args = _parser().parse_args(argv)
    handler = {
        "sync-labels": cmd_sync_labels,
        "list": cmd_list,
        "open": cmd_open,
        "touch": cmd_touch,
        "close": cmd_close,
        "verify": cmd_verify,
    }[args.cmd]
    try:
        return handler(args, tools or FindingsTools())
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
    raise SystemExit(main())
