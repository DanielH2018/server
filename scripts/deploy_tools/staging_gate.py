#!/usr/bin/env python3
"""Ask the staging cluster whether it accepts a commit, from daniel-box.

SLICE 1 OF PHASE C (docs/staging-phase-c.md). This wires to nothing: it deploys a named SHA to
`daniel-stage` and reports a verdict. `gitops_deploy.py` does not call it, and gating on it is
slice 4. What lands here is the piece every later slice needs — a way for the deployer's host to
get an answer out of a cluster it cannot reach.

WHY AN SSH HOP AT ALL. `gitops-deploy.service` runs on daniel-box (`has_gitops` is true there and
nowhere else). `daniel-stage` sits on a libvirt NAT network inside daniel-server and is reachable
from that host only — deliberately, because a staging cluster that could announce on the prod L2
is the one design where staging can hurt prod. So the gate cannot be a step added to
`gitops_deploy.main()`; something has to cross a host boundary. Decision 1 of the Phase C spec
picks this shape (daniel-box shells to daniel-server) over a second deployer publishing a verdict
file, whose stale-pass failure mode is worse than having no gate, and over routing staging to
daniel-box, which would widen the surface the egress fence narrowed.

THE VERDICT VOCABULARY IS THE POINT. Three outcomes, not two, because Decision 4 needs an alert
that separates "staging rejected this change" from "staging could not be asked". A guest that
will not boot, a dirty tree on daniel-server, an expired ssh key and a genuine bad manifest all
look identical if they collapse into a single non-zero exit — and an operator who cannot tell
them apart learns to override on reflex.

ONE SSH CALL, NOT FOUR. The fetch, the cleanliness check, the fast-forward and the deploy are
one remote script piped over `bash -s` rather than four hops. The original reason was `ufw
limit ssh`, which REJECTs the 6th connection in 30s; daniel-box is inside `lan_subnet` and the
LAN is now exempt from that limiter, so the sharp edge is gone. Keep the single call anyway —
four hops is four chances for the transport to fail mid-prep, and a partial prep is a
NO_VERDICT that reads like a staging outage.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Verdicts. These are this script's own exit codes and are what a later slice branches on.
PASS = 0
REJECTED = 1
NO_VERDICT = 2

# The remote's prep-failure code, defined in staging_gate_remote.sh. Kept in sync by
# test_the_prep_code_matches_the_remote_script rather than by hoping.
PREP_FAILED = 70

# The restricted key's dispatcher refusing the request outright — a malformed operation name,
# a SHA that is not a 40-hex object name, tags outside its charset. Defined in
# roles/setup/hypervisor/templates/staging-gate-dispatch.sh.j2 and pinned to this constant by
# ansible/tests/test_staging_gate_dispatch.py.
#
# It classifies as NO_VERDICT for the same reason PREP_FAILED does: the gate could not be
# ASKED. Reading a malformed request as "staging rejected this change" would fail a merge for
# a reason that has nothing to do with the merge.
DISPATCH_REFUSED = 71

# deploy.sh exit codes that all mean *nothing was deployed*, so staging never formed an opinion:
# 2 = a tag matched no service, 3 = the change is broad, 4 = the tree is behind origin,
# 75 = the git-tree lock stayed busy. Reading any of these as a rejection would fail a merge for
# a reason that has nothing to do with the merge.
DEPLOY_SH_NO_VERDICT = frozenset({2, 3, 4, 75})

# ssh's own failure code. A remote command exiting 255 is indistinguishable from ssh failing to
# connect, and that ambiguity resolves toward NO_VERDICT on purpose: deploy.sh never exits 255,
# so in practice this is the transport.
SSH_FAILURE = 255

REMOTE_HOST = "daniel-server"
REMOTE_SCRIPT = Path(__file__).resolve().parent / "staging_gate_remote.sh"

# The staging subset (docs/staging-cluster.md, Decision 6). A caller may narrow this; it may not
# widen it to a service the cluster does not run, which would exit 2 on the far side as a tag
# that matched nothing — a NO_VERDICT that reads like a broken gate.
STAGING_SERVICES = (
    "traefik",
    "authelia",
    "freshrss",
    "node-exporter",
    "registry",
    "ical-proxy",
)


def classify(rc: int) -> int:
    """Map the remote script's exit code to a verdict.

    Pure, so the rejecting half of the test suite can drive the same function the runner drives
    instead of asserting arithmetic of its own.
    """
    if rc == PASS:
        return PASS
    if rc in (PREP_FAILED, DISPATCH_REFUSED, SSH_FAILURE) or rc in DEPLOY_SH_NO_VERDICT:
        return NO_VERDICT
    return REJECTED


def verdict_name(verdict: int) -> str:
    return {PASS: "PASS", REJECTED: "REJECTED", NO_VERDICT: "NO_VERDICT"}[verdict]


def run_gate(sha: str, tags: str, timeout: float) -> int:
    """Deploy `sha` to staging over ssh and return the remote's raw exit code."""
    script = REMOTE_SCRIPT.read_text()
    try:
        completed = subprocess.run(
            # ServerAlive* is the fix for M-5, and it is not cosmetic. When the transport wedges,
            # the local timeout below kills ssh here — but the remote `bash -s` is a different
            # process group on a different host, so nothing local reaches it and it keeps holding
            # /var/lock/staging-gate.lock. The next tick then answers PREP_FAILED on lock
            # contention, which is NO_VERDICT, which is silent. Making the CLIENT notice a dead
            # connection is what lets sshd reap the remote side: three missed 15s probes tears the
            # session down at ~45s, well inside the 1800s timeout, so the orphan is cleaned up by
            # the far end rather than left for someone to find.
            [
                "ssh",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                REMOTE_HOST,
                "bash",
                "-s",
                "--",
                sha,
                tags,
            ],
            input=script,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A timeout is not a rejection. The play carries its own rollout and stabilisation
        # deadlines, so reaching this one means the transport or the host wedged, not that the
        # manifests are bad.
        print(
            f"staging-gate: no answer within {timeout:.0f}s — treating as NO_VERDICT",
            file=sys.stderr,
        )
        return SSH_FAILURE
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="the commit to deploy to staging")
    parser.add_argument(
        "--tags",
        default=",".join(STAGING_SERVICES),
        help="comma-separated deploy tags (default: the whole staging subset)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="seconds to wait for the remote deploy (default: 1800)",
    )
    args = parser.parse_args()

    rc = run_gate(args.sha, args.tags, args.timeout)
    verdict = classify(rc)
    print(
        f"staging-gate: {verdict_name(verdict)} (remote exit {rc}) for {args.sha[:8]}"
    )
    return verdict


if __name__ == "__main__":
    sys.exit(main())
