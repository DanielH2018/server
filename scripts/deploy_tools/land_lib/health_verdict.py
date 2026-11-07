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
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import UNREADABLE_GATE_NOTE, Verdict, say


def _primary_carries(ln: Landing, sha: str) -> bool:
    """Does the primary checkout contain `sha`?"""
    return ln.git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0


def _gated_tree(ln: Landing) -> str:
    """The commit the gate renders from, or "" for the primary checkout as it stands.

    `deployed_at` once the promotion step has run. Otherwise the merge commit, when a
    promotion applied any gated tag: `probe.py health` reads `owner` from the tree it runs in
    (`lib.k8s_roles.tag_owners`), and only a tree carrying the flip knows Flux applies the
    tag. From a tree before the flip, the probe looks a CRD-only tag up by name (#5744).
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
    A landing never moves the primary checkout (`deployed_at`), so
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
        # land.sh's own checkout may predate the merge and see none of a role it adds.
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


def health(ln: Landing) -> NoReturn:
    """Gate every deployed tag, then settle, or name what is still open."""
    pr, sha, tags = ln.opts.pr, ln.merge_sha, ",".join(promotion.gated_tags(ln))
    done = applied_wording(ln)
    result = gate_from_the_deployed_tree(ln)
    for line in result.lines:
        say(line)
    if ln.plane:
        print(f"  STILL UNAPPLIED, and no deploy tag covers it: {ln.plane}")
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
            f"PR #{pr}, {sha} — {done}, the plane above not",
        )
    hosts = ", ".join(t.host for t in ln.host_targets)
    ln.finish(
        Verdict.SETTLED,
        0,
        f"PR #{pr}, {sha}, tags: {tags}" + (f", hosts: {hosts}" if hosts else ""),
    )
