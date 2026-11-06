"""Step 7: the health verdict, and the two halves a healthy deploy can still leave open.

The gate is the half `ansible-playbook` exiting 0 cannot speak to: readiness flips a
Deployment to Available before a bad liveness probe starts killing it. It is asked with
--no-post semantics -- the verdict returns to the session, not to Discord, where the
--detach path already reports.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.deploy_lib.detach_notify import GateResult
from deploy_tools.land_lib import promotion, tick
from deploy_tools.land_lib.landing import Landing, TickState
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    UNREADABLE_GATE_NOTE,
    Cause,
    Verdict,
    hold_clear_hint,
    remaining_hosts_note,
    say,
)


def _primary_carries(ln: Landing, sha: str) -> bool:
    """Does the primary checkout contain `sha`?"""
    return ln.git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0


def _gated_tree(ln: Landing) -> str:
    """The commit the gate renders from, or "" for the primary checkout as it stands.

    `deployed_at` when no tick was awaited. Otherwise the merge commit, when a
    promotion applied any gated tag: `probe.py health` reads `owner` from the tree it runs in
    (`releases_retired.tree_owners`), and only a tree carrying the flip knows Flux applies the
    tag. From a tree before the flip, the probe grades the tag against Ansible's frozen
    release record and fails it NOT ROLLED, and it looks a CRD-only tag up by name (#5744).
    """
    if ln.deployed_at:
        return ln.deployed_at
    flux = set(ln.flux_tags)
    if any(tag in flux for tag in promotion.gated_tags(ln)):
        return ln.merge_sha
    return ""


def gate_from_the_deployed_tree(ln: Landing) -> GateResult:
    """Ask the health gate, rendering the role's manifests from the tree that landed.

    `probe.py health <tag>` enumerates the workloads to gate by rendering the role's
    manifests, from the checkout it is invoked in, and reads each tag's `owner` from it too.
    A landing that awaited no tick (`deployed_at`) has not moved the primary checkout, so
    gating from there enumerates nothing for a role that commit ADDS and the whole gate reads
    `skipped`. A landing whose promotion applied a tag gates from its merge commit for the same
    reason (`_gated_tree`). With neither, the gate runs in the primary, never in land.sh's own
    checkout, which may predate the merge.

    A snapshot that could not be taken FAILS THE GATE, unless the primary already contains the
    commit. Falling back to the primary unconditionally is what the first cut did, and it
    answers from the one tree that cannot see a role this commit adds: `check_one` returns
    `skipped` and the landing reads `settled` having gated nothing. An equivalent-or-newer
    primary is the single case where the fallback still answers the right question, and the
    gate is then run IN the primary, not in land.sh's own checkout, which may predate it.
    """
    at = _gated_tree(ln)
    if not at:
        # The tick fast-forwarded the primary. land.sh's own checkout may predate the merge and
        # see none of a role it adds.
        return ln.tools.gate(promotion.gated_tags(ln), cwd=ln.opts.primary)
    with ln.tools.snapshot(ln.opts.primary, at) as snap:
        if snap is not None:
            return ln.tools.gate(promotion.gated_tags(ln), cwd=snap)
        if _primary_carries(ln, at):
            say(
                f"could not snapshot {at[:12]} for the health gate; the primary "
                "checkout already carries it, so rendering there instead"
            )
            return ln.tools.gate(promotion.gated_tags(ln), cwd=ln.opts.primary)
        return GateResult(
            False,
            [
                f"could not snapshot {at[:12]} for the health gate, and the primary "
                "checkout does not carry it yet -- nothing here can enumerate what landed, "
                "so this landing is NOT reported healthy. Re-run it once the tick has "
                "fast-forwarded, or check the workloads by hand."
            ],
        )


def applied_wording(ln: Landing) -> str:
    """What the landing applied, for the verdict lines: services, hosts, or both.

    A hosts-only landing, one with host targets and no service tag, never reads "services
    deployed", whatever else the tick applied for it.
    """
    services = bool(promotion.gated_tags(ln))
    if not ln.host_targets:
        return "services deployed"
    return "services deployed and hosts applied" if services else "hosts applied"


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, tags = ln.opts.pr, ln.merge_sha, ",".join(promotion.gated_tags(ln))
    done = applied_wording(ln)
    result = gate_from_the_deployed_tree(ln)
    for line in result.lines:
        say(line)
    # Before any verdict, so every exit below has asked: the gate is the wait that lets a
    # joined tick end (`tick.rearm_tick`).
    tick.rearm_tick(ln)
    if ln.plane:
        print(f"  STILL UNAPPLIED, and no deploy tag covers it: {ln.plane}")
        # Both remediations, because this arm ends the landing and the one at the foot of
        # this function never runs.
        if ln.classification.remaining_setup:
            print(remaining_hosts_note(ln.classification.remaining_setup))
        # The tick's own half too: a PR carrying BOTH ends here without ever reading the
        # deployer's state, so every tick state went unreported. Reported,
        # not re-verdicted — `Landing.tick_half_open_lines` carries why.
        for line in ln.tick_half_open_lines():
            print(line)
    if result.unreadable:
        print(UNREADABLE_GATE_NOTE)
        ln.finish(
            Verdict.DEFERRED,
            75,
            f"PR #{pr}, {sha}, tags: {tags} — deployed, but the health gate could not "
            "read its store",
        )
    if not result.settled:
        ln.finish(Verdict.UNHEALTHY, 1, f"PR #{pr}, {sha}, tags: {tags}")
    if ln.plane:
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — {done}, the plane above not"
            + (
                ", nor the hosts beside it" if ln.classification.remaining_setup else ""
            ),
        )
    # Only when the tick applies part of this PR itself does its state speak to THIS
    # landing; for an ordinary service PR, behind_since is somebody else's merge.
    if ln.classification.self_applied:
        state = ln.tick_state()
        if state == TickState.UNKNOWN:
            print(
                f"  {done}, but the deployer's state directory "
                f"({ln.opts.deployer_state}) could not be read, so whether the tick "
                "applied its own half is unknown"
            )
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha}, tags: {tags} — {done}, the tick's half unknown",
            )
        if state == TickState.HELD:
            hold = ln.state("hold_sha")
            print(
                f"  {done}, but the deployer is holding {hold}: "
                "its own apply failed — see the held planes in owed.jsonl"
            )
            print(f"  {hold_clear_hint(hold)}")
            ln.ledger.cause = Cause.TICK_HELD
            ln.finish(
                Verdict.DEPLOY_FAILED,
                1,
                f"PR #{pr}, {sha} — {done}, the tick's apply is held",
            )
        if state == TickState.BEHIND:
            print(
                f"  {done}, but the tick has not fast-forwarded to origin "
                f"(parked since: {ln.state('behind_since')})"
            )
            if ln.tick_watch_abandoned:
                print(ABANDONED_WATCH_NOTE)
                ln.finish(
                    Verdict.DEFERRED,
                    75,
                    f"PR #{pr}, {sha}, tags: {tags} — {done}; this run stopped "
                    "watching a tick still applying, so the tick's half is unsettled",
                )
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr}, {sha}, tags: {tags} — {done}, the tick's half not yet",
            )
        # CONVERGED says the tick is not deferring this PR, which any session's `git merge
        # --ff-only` also produces — and once local == origin the tick returns `noop` forever,
        # so a plane it never applied is stranded while this reads `settled`.
        if not ln.tick_applied(sha):
            print(
                f"  {done}, but the tick recorded no broad apply covering this PR "
                f"({ln.receipt_summary(sha)})"
            )
            # The same hole `deploy.no_tag_outcome` carries, one arm over and for the same
            # reason: the receipt's `applied` is written only after the apply returns, and a tick that
            # already ff-merged this PR answers CONVERGED rather than BEHIND, so the
            # abandoned-watch arm above never sees it.
            if ln.tick_watch_abandoned:
                print(ABANDONED_WATCH_NOTE)
                ln.finish(
                    Verdict.DEFERRED,
                    75,
                    f"PR #{pr}, {sha}, tags: {tags} — {done}; this run stopped "
                    "watching a tick still applying, so whether it recorded an apply of "
                    "this PR is not yet known",
                )
            for line in ln.tick_half_remediation():
                print(line)
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha}, tags: {tags} — the tick converged without applying this PR",
            )
    if ln.classification.remaining_setup:
        local = ln.tools.hostname()
        # The note says which hosts and why for each role it names, including the repo-file
        # case where the tick applied the role on NO host -- so this line states what
        # was deployed and hands the rest to the note, rather than asserting a self-apply on
        # `local` that a repo-file-only role never had.
        note = remaining_hosts_note(ln.classification.remaining_setup).lstrip()
        print(f"  {done}, and {note}")
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha}, tags: {tags} — self-applied on {local} only; "
            "other hosts still need it",
        )
    hosts = ", ".join(t.host for t in ln.host_targets)
    ln.finish(
        Verdict.SETTLED,
        0,
        f"PR #{pr}, {sha}, tags: {tags}" + (f", hosts: {hosts}" if hosts else ""),
    )
