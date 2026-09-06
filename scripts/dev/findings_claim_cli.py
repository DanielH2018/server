"""The four claim subcommands: `claim`, `release`, `claims` and `reap`.

Split out of `findings.py` to keep that file under its 600-line cap, the same reason
`findings_cli.py` exists. These four belong together for a better reason than size, though:
they are the only handlers that read the WORKTREES, and each takes a different position on
what a FAILED read means. `reap` refuses outright, because it writes on that read and must
not treat an error as "every worktree is gone". `claims` renders anyway and says on stderr
that the verdict is a guess. `claim` warns and proceeds — its stale-at-birth guard is
advisory, and a stale claim already sitting on an issue is left standing rather than reaped
on a guess. Keeping the four in one file keeps those three positions readable against each
other.

The layering is the same as elsewhere in this package: `findings_claim.py` decides whether a
claim is live, `findings_plans.py` turns a decision into gh argv, `findings_gh.py` runs them,
and this file is the CLI over the three.
"""

import argparse
import json
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# DECIDED: imported as `dev.<leaf>`, never as bare siblings — see the marker in findings.py.
from dev.findings_claim import (
    another_claim_blocks,
    claim_is_live,
    claim_states,
    stale_holder,
)
from dev.findings_gh import _existing_labels, _load_issue, load_issues, run
from dev.findings_model import current_claim, now_iso, validate_worktree_name
from dev.findings_plans import ClaimRefused, plan_claim, plan_release, plan_sync_labels
from dev.findings_tools import FindingsTools


def cmd_claim(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Claims one or more issues for a worktree, reaping a stale claim that blocks one.

    Takes every issue it can and refuses the rest, rather than refusing the whole batch on
    one bad issue: a fan-out claims several issues at once, and losing four good claims
    because the fifth is `manual` would mean re-running and re-reading everything.

    REAP-THEN-CLAIM. `next` offers an issue whose claim is stale — deliberately, since that
    is what `reap` exists to clear — but `plan_claim` refused ANY claim, live or not. So a
    session did what `next` told it to and got exit 3 with no route forward, and nothing in
    the repo invoked `reap` to clear the claim (#1274). A stale claim is now released here,
    under the reason `reap` would have given, and the claim proceeds.

    STALE AT BIRTH. One guard covers #1278 and #1281, because both are the same question
    asked of the same value: would the claim this command is about to write read as stale
    the moment it lands? `claim_is_live` answers it whole — a `--worktree` naming no branch
    at all ("no worktree — the claim names a branch nothing has checked out", which is what
    `claim 1132 --worktree issue-1132` gets when the branch is `worktree-issue-1132`), and a
    `--worktree` naming a real branch whose state is REMOVABLE (`master`, or a resumed
    orchestrator that came back without the lock its claims leaned on). Both end the same
    way: `next` re-offers the issue and `reap` releases it while the session is still working
    it, which is the exact double-assignment the protocol exists to prevent. Two guards with
    two exit codes would be worse than the bug, so there is one, and it exits 3 like every
    other "nothing was written" refusal. `--force` is the way past it, because the resumed
    orchestrator is a legitimate case and a one-way door is not acceptable here.

    Returns 0 when every issue was taken, 3 when any was refused.
    """
    # Hoisted out of the loop: `args.worktree` does not vary across the batch, so the guard
    # below needs the read once, and the reap path further down reuses the same tuple. That
    # retires the lazy read this function used to do — its point was to skip several git
    # calls per registered worktree on a batch where nothing was blocked, and the guard
    # needs them on every batch anyway.
    bad = validate_worktree_name(args.worktree)
    if bad:
        sys.stderr.write(
            f"claim: --worktree `{args.worktree}` {bad}. The claim comment and the label "
            "would be written and the trailer would then fail to parse on read-back, which "
            "reported a race against nobody (#1284).\n"
        )
        return 2
    facts = tools.worktree_facts()
    trees, dirty, merged, ok = facts
    if ok:
        live, why = claim_is_live(args.worktree, trees, dirty, merged)
        if not live and not args.force:
            sys.stderr.write(
                f"claim: `{args.worktree}` would be stale at birth — {why}\n"
                "`next` would re-offer these issues and `reap` would release them while "
                "you work them. Pass --force to claim anyway.\n"
            )
            return 3
    else:
        # Advisory, so it fails OPEN — the opposite direction from `cmd_reap`, and for the
        # opposite reason. Reap WRITES on a bad read; this only declines to warn, and a
        # transient git error must not stop a session claiming work at all. One warning per
        # batch, covering both things the read would have decided: whether THIS claim reads
        # stale, and whether a stale claim already on an issue can be reaped out of the way.
        sys.stderr.write(
            "warning: worktree read failed; not checking whether this claim reads stale, "
            "and a stale claim will refuse rather than reap\n"
        )
    # `gh issue edit --add-label` fails on a label the repo lacks, which is why cmd_close
    # syncs first. Without this the FIRST claim posts its comment, fails the label edit and
    # exits 1; the retry then reads "already claimed" and the label is never added at all.
    run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    refused = False
    for number in args.numbers:
        issue = _load_issue(number, tools)
        if another_claim_blocks(issue, args.worktree):
            # Same fail-safe direction `cmd_reap` takes: a transient git error must not read
            # as "every worktree is gone". The claim blocks, as it did before this path
            # existed, and `plan_claim` refuses it below. The warning was said once, above.
            stale = stale_holder(issue, trees, dirty, merged) if ok else None
            if stale:
                holder, why = stale
                run(
                    plan_release(
                        issue,
                        worktree=holder,
                        when=now_iso(),
                        reason=f"reaped by `{args.worktree}`: {why}",
                    ),
                    args.dry_run,
                    tools,
                )
                print(f"#{number} reaped stale claim by `{holder}` — {why}")
                if args.dry_run:
                    # The release was printed, not posted, so a re-read still shows the old
                    # claim and `plan_claim` would refuse something that will really succeed.
                    print(f"#{number} would be claimed by `{args.worktree}`")
                    continue
                # Re-read so `plan_claim` sees the release comment just posted.
                issue = _load_issue(number, tools)
        try:
            plans = plan_claim(
                issue, worktree=args.worktree, session=args.session, when=now_iso()
            )
        except ClaimRefused as exc:
            print(f"#{number} refused: {exc.reason}")
            refused = True
            continue
        already_mine = current_claim(issue) == args.worktree
        if not plans:
            print(f"#{number} already claimed by `{args.worktree}`")
            continue
        if already_mine:
            # A reclaim planning something is `plan_claim` repairing a `claimed` label a
            # failed `--add-label` left off (#1277). No comment is posted, so there is no
            # race to read back for — and saying "claimed" would misreport a repair as a
            # first claim.
            run(plans, args.dry_run, tools)
            print(
                f"#{number} already claimed by `{args.worktree}`; "
                f"{'would repair' if args.dry_run else 'repaired'} the `claimed` label"
            )
            continue
        run(plans, args.dry_run, tools)
        if args.dry_run:
            print(f"#{number} would be claimed by `{args.worktree}`")
            continue
        # Read back. Two sessions can both post a claim comment; the FIRST one holds it,
        # and without this the loser prints success and starts work on someone else's issue.
        winner = current_claim(_load_issue(number, tools))
        if winner is None:
            # NOT a race. The comment was posted and the read-back found nothing holding the
            # issue at all, so the READ is what failed — an unparseable trailer, or a
            # comment past gh's 100-comment page cap. Reporting that as `lost the race to
            # \`None\`` told the operator they lost to a rival that does not exist (#1284).
            print(
                f"#{number} claim posted, but the read-back found no claim at all — the "
                "trailer did not parse, or the comment is past gh's comment page cap"
            )
            refused = True
            continue
        if winner != args.worktree:
            print(f"#{number} lost the race to `{winner}`")
            refused = True
            continue
        print(f"#{number} claimed by `{args.worktree}`")
    return 3 if refused else 0


def cmd_release(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Releases this worktree's claim on one or more issues."""
    bad = validate_worktree_name(args.worktree)
    if bad:
        sys.stderr.write(f"release: --worktree `{args.worktree}` {bad}\n")
        return 2
    # The same label sync `cmd_claim` makes, for the mirror-image reason (#1284): `gh issue
    # edit --remove-label` fails on a label the repo does not have, and a claim can arrive
    # with the label never created — hand-posted, or after an `--add-label` that failed.
    # Without this the release comment is posted and THEN the label edit exits 1, leaving
    # the claim released in the fold and the label standing.
    run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    refused = False
    for number in args.numbers:
        issue = _load_issue(number, tools)
        try:
            plans = plan_release(
                issue, worktree=args.worktree, when=now_iso(), reason=args.reason
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
    trees, dirty, merged, ok = tools.worktree_facts()
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
    trees, dirty, merged, ok = tools.worktree_facts()
    if not ok:
        # Must not read a git failure as "every worktree is gone" and release everything.
        print("reap: could not read git; refusing to release anything")
        return 1
    # Same reason as `cmd_release`: a `--remove-label` on a label the repo lacks fails after
    # the release comment is already posted, leaving the reap half-applied (#1284).
    run(plan_sync_labels(_existing_labels(tools)), args.dry_run, tools)
    issues = load_issues("open", tools)
    by_number = {i["number"]: i for i in issues}
    reaped = 0
    for s in claim_states(issues, trees, dirty, merged):
        if s.live:
            continue
        plans = plan_release(
            by_number[s.number],
            worktree=s.worktree,
            when=now_iso(),
            reason=f"reaped: {s.reason}",
        )
        run(plans, args.dry_run, tools)
        verb = "would be released" if args.dry_run else "released"
        print(f"#{s.number} {verb} — {s.reason}")
        reaped += 1
    verb = "would be released" if args.dry_run else "released"
    print(f"reap: {reaped} stale claim(s) {verb}")
    return 0
