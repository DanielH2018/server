"""Step 6: the health verdict, and the plane a healthy deploy can still leave open.

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
from deploy_tools.land_lib import promotion
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import (
    STALE_GATE_NOTE,
    UNREADABLE_GATE_NOTE,
    Verdict,
    say,
)

# How long the gate is asked again while its store is unreadable, and how often. Kept inside
# `promotion.DEADLINE_RESERVE_S` with room for the gate's own run.
UNREADABLE_RETRY_S = 300
UNREADABLE_POLL_S = 30


def _primary_carries(ln: Landing, sha: str) -> bool:
    """Does the primary checkout contain `sha`?"""
    return ln.git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0


def _gated_tree(ln: Landing) -> str:
    """The commit the gate renders from, or "" for the primary checkout as it stands.

    `deployed_at` once the promotion step has run. Otherwise the merge commit, when a
    promotion applied any gated tag: `probe.py health` reads which tags Flux applies from the
    tree it runs in, and only a tree carrying the flip knows Flux applies the tag. From a tree before the flip, the probe looks a CRD-only tag up by name (#5744).
    """
    if ln.deployed_at:
        return ln.deployed_at
    flux = set(ln.flux_tags)
    if any(tag in flux for tag in promotion.gated_tags(ln)):
        return ln.merge_sha
    return ""


def _advanced_to_origin(ln: Landing, at: str) -> str:
    """`origin/master`'s tip when it contains `at`, else `at` itself.

    `probe.py health` refuses a role whose files `origin/master` changed after its tree's HEAD
    (`health_checkout.stale_checkout_reason`), so a gate pinned to the merge commit refuses
    every role a later master commit touched, on every re-run (#7816). The snapshot shares the
    primary's refs, so a tree at the very tip this reads leaves the probe nothing newer to find.
    No fetch runs between the two reads for that reason.

    DECIDED: the tip may hold a role change `promote` has not applied yet, and the gate then
    grades the live workload against that newer definition. The probe refuses every older
    tree outright, so the trade is a verdict against master's definition or none at all.
    """
    tip = ln.git("rev-parse", f"origin/{BRANCH}")
    sha = tip.stdout.strip() if tip.returncode == 0 else ""
    if not sha or sha == at:
        return at
    if ln.git("merge-base", "--is-ancestor", at, sha).returncode != 0:
        return at
    say(f"gating from origin/{BRANCH} at {sha[:12]}, which contains {at[:12]}")
    return sha


def gate_from_the_deployed_tree(ln: Landing) -> GateResult:
    """Ask the health gate, rendering the role's manifests from the tree that landed.

    `probe.py health <tag>` enumerates the workloads to gate by rendering the role's
    manifests, from the checkout it is invoked in, and reads each tag's `owner` from it too.
    A landing never moves the primary checkout (`deployed_at`), so
    gating from there enumerates nothing for a role that commit ADDS and the whole gate reads
    `skipped`. A landing whose promotion applied a tag gates from its merge commit for the same
    reason (`_gated_tree`). With neither, the gate runs in the primary, never in land.sh's own
    checkout, which may predate the merge. The snapshot is taken at `origin/master` when that
    contains the commit (`_advanced_to_origin`).

    A snapshot that could not be taken FAILS THE GATE, unless the primary already contains the
    commit. Falling back to the primary unconditionally is what the first cut did, and it
    answers from the one tree that cannot see a role this commit adds: `check_one` returns
    `skipped` and the landing reads `settled` having gated nothing. An equivalent-or-newer
    primary is the single case where the fallback still answers the right question, and the
    gate is then run IN the primary, not in land.sh's own checkout, which may predate it.
    """
    at = _gated_tree(ln)
    if not at:
        # land.sh's own checkout may predate the merge and see none of a role it adds.
        return ln.tools.gate(promotion.gated_tags(ln), cwd=ln.opts.primary)
    # The fallback below still asks for `at`: a primary carrying what landed can answer, and
    # the probe itself refuses a role that primary holds an older definition of.
    with ln.tools.snapshot(ln.opts.primary, _advanced_to_origin(ln, at)) as snap:
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
                "so this landing is NOT reported healthy. Re-run it once the primary "
                "checkout has fast-forwarded, or check the workloads by hand."
            ],
        )


def applied_wording(ln: Landing) -> str:
    """What the landing applied, for the verdict lines: services, hosts, or both.

    A hosts-only landing, one with host targets and no service tag, never reads "services
    deployed". Neither does a landing whose every tag
    is suspended and which has no host target.
    """
    services = bool(promotion.gated_tags(ln))
    if not ln.host_targets:
        return "services deployed" if services else "no service deployed"
    return "services deployed and hosts applied" if services else "hosts applied"


def _gate_until_readable(ln: Landing) -> GateResult:
    """The gate's answer, asked again while its store reads unreadable, up to the bound.

    DECIDED: an unreadable store is retried before the landing defers. The PRs that hit it
    roll the store the gate reads (observability, monitor-bridge, loki-homelab): 29 landings
    in the 14 days to 2026-11-09 deferred on it, and 22 were never re-run, so their deploys
    went ungraded. The `deferred` outcome of #5543 still ends a store unreadable past the bound.
    A stale tree is not retried: asking again reads the same tree and refuses again (#7816).
    """
    give_up_at = ln.tools.clock() + UNREADABLE_RETRY_S
    result = gate_from_the_deployed_tree(ln)
    while result.unreadable and not result.stale and ln.tools.clock() < give_up_at:
        say(
            "the health gate could not read its store; asking again in "
            f"{UNREADABLE_POLL_S}s"
        )
        ln.tools.sleep(UNREADABLE_POLL_S)
        result = gate_from_the_deployed_tree(ln)
    return result


# The most tags a VERDICT line lists; a broader landing's line counts them, and
# `--verdict-file` keeps the list.
VERDICT_TAGS_LISTED_UP_TO = 10


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, gated = ln.opts.pr, ln.merge_sha, promotion.gated_tags(ln)
    listed = ",".join(gated)
    shown = (
        f"{len(gated)} (broad)" if len(gated) > VERDICT_TAGS_LISTED_UP_TO else listed
    )
    done = applied_wording(ln)

    def end(verdict: str, rc: int, tail: str = "") -> NoReturn:
        ln.finish(
            verdict,
            rc,
            f"PR #{pr}, {sha}, tags: {shown}{tail}",
            recorded=f"PR #{pr}, {sha}, tags: {listed}{tail}",
        )

    result = _gate_until_readable(ln)
    for line in result.lines:
        say(line)
    if ln.plane:
        print(f"  STILL UNAPPLIED, and no deploy tag covers it: {ln.plane}")
    if result.stale:
        print(STALE_GATE_NOTE)
        end(
            Verdict.DEFERRED,
            75,
            " — deployed, but the health gate's tree was older than origin/master",
        )
    if result.unreadable:
        print(UNREADABLE_GATE_NOTE)
        end(
            Verdict.DEFERRED,
            75,
            " — deployed, but the health gate could not read its store",
        )
    if not result.settled:
        end(Verdict.UNHEALTHY, 1)
    if ln.plane:
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — {done}, the plane above not",
        )
    hosts = ", ".join(t.host for t in ln.host_targets)
    end(Verdict.SETTLED, 0, f", hosts: {hosts}" if hosts else "")
