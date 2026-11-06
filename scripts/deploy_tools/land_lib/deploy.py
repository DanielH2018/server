"""Step 5: wait on the promotion that applies this PR, or say why there is nothing to wait on.

No landing runs deploy.sh. Every `containers_list` entry is `platform: k8s`, so `promote` and
Flux apply every tag, and a host's package reaches it through the host wait in step 6.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import DEPLOY_BROAD, DEPLOY_OK
from deploy_tools.land_lib import promotion, tick
from deploy_tools.land_lib.landing import Landing, TickState
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    Cause,
    Verdict,
    hold_clear_hint,
    host_not_waited_note,
    remaining_hosts_note,
    say,
)


def derive_from_diff(ln: Landing) -> None:
    """Resolve the fallback to a tag list HERE rather than handing deploy.sh --changed.

    deploy.sh resolves --changed internally, so the verdict call would receive an empty
    --tags and report settled having checked nothing -- on the large-PR path, where
    verification matters most.

    A range `changed` refuses as broad derives no tag here. Its k8s half is the promotion's,
    and land-wait's changed set names the tags this landing gates. `changed` prints what its
    Pi and setup halves owe on stderr, beside the verdict.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.tools.deploy_tags(ln.opts.primary, ["changed", ln.opts.since])
    if r.returncode == DEPLOY_BROAD:
        say(
            "`changed` refused the range as broad; the promotion's changed set names its k8s "
            "tags, and the host wait covers a change to a host's package"
        )
        ln.resolved_tags = []
        return
    if r.returncode != DEPLOY_OK:
        ln.die(f"deploy_tags.py changed failed (exit {r.returncode})", 1)
    # An empty element (a doubled or trailing comma) carries no service, so reading it as a
    # tag would end the landing as a tag miss with nothing wrong.
    ln.resolved_tags = [t for t in r.stdout.strip().split(",") if t]


def no_tag_outcome(ln: Landing, scope: str = "no service tag") -> NoReturn:
    """No service tag: owed to a hand, nothing at all, or the tick's own state decides.

    `scope` is why this landing has no tags to deploy, printed on the verdict line. The
    default is the ordinary case — the PR's own files derived none. `derive_from_diff` passes
    its own wording, because a refused broad derivation is not the same fact and an operator
    reading `reaches no service tag` about a 330-file PR goes looking for a different bug.
    """
    pr, sha = ln.opts.pr, ln.merge_sha
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
        # Both remediations, because this arm ends the landing and the one at the foot of
        # this function never runs.
        if ln.classification.remaining_setup:
            print(remaining_hosts_note(ln.classification.remaining_setup))
        # The tick's own half too: a PR carrying BOTH ends here without ever reading the
        # deployer's state, so every tick state would go unreported. Reported,
        # not re-verdicted — `Landing.tick_half_open_lines` carries why.
        for line in ln.tick_half_open_lines():
            print(line)
        if ln.host_targets:  # this arm ends the landing before the host wait
            print(host_not_waited_note(ln.host_targets))
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} reaches {scope}, but is not done",
        )
    if not ln.classification.self_applied:
        if ln.classification.remaining_setup:
            # No plane, no tag, and nothing the tick applies -- and still a host owes a hand.
            # A PR whose only loud path is a repo file a setup role ships out of the checkout,
            # with no row in `deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES`,
            # puts no role in the change set, so `self_applied` is False and this arm would
            # end the landing at `nothing-to-deploy` with the remediation computed and never
            # printed.
            print(remaining_hosts_note(ln.classification.remaining_setup))
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr} reaches {scope}, but a setup role ships a changed repo file to "
                "hosts no tick applies",
            )
        ln.finish(Verdict.NOTHING_TO_DEPLOY, 0, f"PR #{pr} touched no service")
    state = ln.tick_state()
    if state == TickState.UNKNOWN:
        print(
            f"  the deployer's state directory ({ln.opts.deployer_state}) could not be read, "
            "so whether the tick applied this PR is unknown"
        )
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} — the deployer's state could not be read; confirm the tick applied it",
        )
    if state == TickState.HELD:
        hold = ln.state("hold_sha")
        print(
            f"  the deployer is holding {hold}: its apply failed — see the held planes in owed.jsonl and the gitops-deploy journal"
        )
        print(f"  {hold_clear_hint(hold)}")
        ln.ledger.cause = Cause.TICK_HELD
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — the tick's own apply failed and is held",
        )
    if state == TickState.BEHIND:
        print(
            f"  the tick did not fast-forward to origin (parked since: {ln.state('behind_since')})"
        )
        if ln.tick_watch_abandoned:
            print(ABANDONED_WATCH_NOTE)
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr} — landed; this run stopped watching a tick still applying, "
                "so whether the deployer is deferring or holding is not yet known",
            )
        print(
            "  Usually a newer merge whose CI is still running; the next tick crosses it. Nothing is wrong with this PR."
        )
        ln.finish(
            Verdict.DEFERRED, 75, f"PR #{pr} — landed, not yet applied by the tick"
        )
    if not ln.tick_applied(sha):
        print(
            "  the tick crossed this PR but recorded no broad apply covering it "
            f"({ln.receipt_summary(sha)})"
        )
        # The receipt's `applied` is written by `handle_broad` only after the apply RETURNS, so a run
        # still applying reads exactly like a tick that converged without applying — and a
        # tick that already ff-merged this PR answers CONVERGED, never BEHIND, so the arm
        # above cannot catch it.
        if ln.tick_watch_abandoned:
            print(ABANDONED_WATCH_NOTE)
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr}, {sha} — this run stopped watching a tick still applying, so "
                "whether it recorded an apply of this PR is not yet known",
            )
        for line in ln.tick_half_remediation():
            print(line)
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — the tick converged without recording an apply of this PR",
        )
    if ln.classification.remaining_setup:
        local = ln.tools.hostname()
        print(
            f"  applied on {local} only; it also reaches: {ln.classification.remaining_setup}"
        )
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — self-applied on {local} only; other hosts still need it",
        )
    ln.finish(
        Verdict.SETTLED,
        0,
        f"PR #{pr}, {sha} — {scope}; the tick applied it and converged with origin",
    )


def deploy_phase(ln: Landing) -> None:
    """Step 5, end to end: derive, split, wait on promotion, stamp the ledger.

    The split comes first (`promotion.split_k8s`), and a tag it leaves behind names no k8s entry,
    so nothing can apply it. Every landing whose PR may move the bundle then waits on the
    promotion, and land-wait's changed set names the tags the gate reads. A PR that moves no
    bundle file, or whose changed set names no tag, ends in `no_tag_outcome`. A plane it also
    carries, such as the rotation note for `secrets.yml`, is reported by the health step after a
    settled wait, and by `wait_on_promotion` when the wait ends the landing.
    """
    if ln.needs_diff:
        derive_from_diff(ln)
    promotion.split_k8s(ln)
    if ln.resolved_tags:
        ln.ledger.tags_label = ln.tags_csv
        ln.ledger.cause = Cause.TAG_MISS
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{ln.opts.pr} — {ln.tags_csv} matched no k8s entry at "
            f"{ln.merge_sha[:12]}, so nothing applies it",
        )
    waits = (
        bool(ln.flux_tags)
        or ln.needs_diff
        or promotion.moves_the_bundle(ln.classification.pr_paths)
    )
    if not waits:
        if ln.host_targets and not ln.plane:
            return  # the tick took the empty change set, then the host wait
        no_tag_outcome(ln)
    ln.ledger.tags_label = ",".join(ln.flux_tags)
    if not ln.tick_is_the_apply:
        # The gate renders the merge commit from a snapshot, and the tick is kicked to converge
        # the primary checkout.
        ln.deployed_at = ln.merge_sha
        tick.kick_tick(ln)
    promotion.wait_on_promotion(ln)
    # Step 5's own time on the board, the promotion wait included.
    ln.ledger.t_deploy = ln.tools.clock()
    if not ln.flux_tags:
        if ln.host_targets and not ln.plane:
            return
        no_tag_outcome(ln)
