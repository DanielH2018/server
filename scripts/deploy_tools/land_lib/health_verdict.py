"""Step 6: the health verdict, and the two halves a healthy deploy can still leave open.

The gate is the half `ansible-playbook` exiting 0 cannot speak to: readiness flips a
Deployment to Available before a bad liveness probe starts killing it. It is asked with
--no-post semantics -- the verdict returns to the session, not to Discord, where the
--detach path already reports.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import tick
from deploy_tools.land_lib.landing import Landing, TickState
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    Cause,
    Verdict,
    hold_clear_hint,
    remaining_hosts_note,
    say,
)


def gate_from_the_deployed_tree(ln: Landing) -> tuple[bool, list[str]]:
    """Ask the health gate, rendering the role's manifests from the tree that was deployed.

    `probe.py health <tag>` enumerates the workloads to gate by rendering the role's
    manifests, from the checkout it is invoked in. A landing that deployed a snapshot of its
    merge commit (`deployed_at`) has not moved the primary checkout, so gating from there
    enumerates nothing for a role that commit ADDS and the whole gate reads `skipped`.

    A snapshot that could not be taken FAILS THE GATE, unless the primary already contains the
    deployed commit. Falling back to the primary unconditionally is what the first cut did, and
    it answers from the one tree that cannot see a role this commit adds: `check_one` returns
    `skipped` and the landing reads `settled` having gated nothing. An equivalent-or-newer
    primary is the single case where the fallback still answers the right question.
    """
    if not ln.deployed_at:
        return ln.tools.gate(ln.resolved_tags)
    with ln.tools.snapshot(ln.opts.primary, ln.deployed_at) as snap:
        if snap is not None:
            return ln.tools.gate(ln.resolved_tags, cwd=snap)
        if ln.merge_applied():
            say(
                f"could not snapshot {ln.deployed_at[:12]} for the health gate; the primary "
                "checkout already carries it, so rendering there instead"
            )
            return ln.tools.gate(ln.resolved_tags)
        return False, [
            f"could not snapshot {ln.deployed_at[:12]} for the health gate, and the primary "
            "checkout does not carry it yet -- nothing here can enumerate what was deployed, "
            "so this landing is NOT reported healthy. Re-run it once the tick has "
            "fast-forwarded, or check the workloads by hand."
        ]


def _regate_after_later_deploys(ln: Landing) -> bool:
    """After a failed gate, gate again if a later deploy re-rolled the workloads; settled?

    A later deploy of the same service can roll its pods under the gate's one sample, once
    this landing's deploy has released the service lock (#3812). This gates again once that
    deploy has ended, so the verdict grades what it left, and a change that is still broken
    fails the second gate too.

    A healthy second gate settles only where every re-rolled tag's later deploy rendered a
    commit that CONTAINS this PR. The tick's rollback redeploys an older pin, and settling on
    its healthy pods would report a change that is no longer live.
    """
    rerolled = ln.tools.later_deploys(ln.resolved_tags, ln.deploy_ended_at)
    if not rerolled:
        return False
    ln.regated = ",".join(rerolled)
    say(
        f"{ln.regated}: a later deploy rolled this out again after this landing's own "
        "deploy; gating the generation that deploy left"
    )
    settled, lines = gate_from_the_deployed_tree(ln)
    for line in lines:
        say(line)
    if not settled:
        return False
    without = {
        tag: commit
        for tag, commit in rerolled.items()
        if not commit
        or ln.git("merge-base", "--is-ancestor", ln.merge_sha, commit).returncode
    }
    for tag, commit in without.items():
        rendered = commit[:12] or "a commit its release record does not name"
        say(
            f"{tag}: the later deploy rendered {rendered}, which is not proved to contain "
            f"{ln.merge_sha[:12]} -- this change may no longer be live"
        )
    return not without


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, tags = ln.opts.pr, ln.merge_sha, ln.tags_csv
    settled, lines = gate_from_the_deployed_tree(ln)
    for line in lines:
        say(line)
    if not settled:
        settled = _regate_after_later_deploys(ln)
        if ln.regated:
            tags += f" (re-gated after a later deploy of {ln.regated})"
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
    if not settled:
        ln.finish(Verdict.UNHEALTHY, 1, f"PR #{pr}, {sha}, tags: {tags}")
    if ln.plane:
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — services deployed, the plane above not"
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
                "  services deployed, but the deployer's state directory "
                f"({ln.opts.deployer_state}) could not be read, so whether the tick "
                "applied its own half is unknown"
            )
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha}, tags: {tags} — services deployed, the tick's half unknown",
            )
        if state == TickState.HELD:
            hold = ln.state("hold_sha")
            print(
                f"  services deployed, but the deployer is holding {hold}: "
                "its own apply failed — see the held planes in owed.jsonl"
            )
            print(f"  {hold_clear_hint(hold)}")
            ln.ledger.cause = Cause.TICK_HELD
            ln.finish(
                Verdict.DEPLOY_FAILED,
                1,
                f"PR #{pr}, {sha} — services deployed, the tick's apply is held",
            )
        if state == TickState.BEHIND:
            print(
                "  services deployed, but the tick has not fast-forwarded to origin "
                f"(parked since: {ln.state('behind_since')})"
            )
            if ln.tick_watch_abandoned:
                print(ABANDONED_WATCH_NOTE)
                ln.finish(
                    Verdict.DEFERRED,
                    75,
                    f"PR #{pr}, {sha}, tags: {tags} — services deployed; this run stopped "
                    "watching a tick still applying, so the tick's half is unsettled",
                )
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr}, {sha}, tags: {tags} — services deployed, the tick's half not yet",
            )
        # CONVERGED says the tick is not deferring this PR, which any session's `git merge
        # --ff-only` also produces — and once local == origin the tick returns `noop` forever,
        # so a plane it never applied is stranded while this reads `settled`.
        if not ln.tick_applied(sha):
            print(
                "  services deployed, but the tick recorded no broad apply covering this PR "
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
                    f"PR #{pr}, {sha}, tags: {tags} — services deployed; this run stopped "
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
        print(f"  services deployed, and {note}")
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha}, tags: {tags} — self-applied on {local} only; "
            "other hosts still need it",
        )
    ln.finish(Verdict.SETTLED, 0, f"PR #{pr}, {sha}, tags: {tags}")
