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

ONE SSH CALL, AND A RESTRICTED ONE. The fetch, the cleanliness check, the fast-forward and the
deploy happen in a single connection rather than four hops — four hops is four chances for the
transport to fail mid-prep, and a partial prep is a NO_VERDICT that reads like a staging outage.

That call no longer pipes a script to `bash -s`. It authenticates with a dedicated key whose
authorized_keys entry pins a forced command, and sends `gate <sha> <tags>` as the request. The
old shape gave anything able to invoke this script a full shell on daniel-server (2026-08-29
review M-3), and a forced command alone would not have fixed it: ssh forwards stdin regardless,
so a far side still reading stdin would have executed whatever was piped.

`IdentitiesOnly=yes` is NOT what stops ssh using a different key — the default identity files
still count as configured. `identity_problem()` is; see its docstring.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import ROLES

# Verdicts. These are this script's own exit codes and are what a later slice branches on.
PASS = 0
REJECTED = 1
NO_VERDICT = 2

# The remote's prep-failure code, defined in staging_gate_remote.sh. Kept in sync by
# test_the_prep_code_matches_the_remote_script rather than by hoping.
PREP_FAILED = 70

# The remote's lock-contention code, also defined in staging_gate_remote.sh and pinned to it by
# test_the_busy_code_matches_the_remote_script. It is in NO_VERDICT_CODES below, so by default
# this script behaves exactly as it did before the code existed — the deployer cannot tell a busy
# lock from any other reason the gate could not be asked, and does not need to.
#
# `--report-busy` is what makes it visible, and only backfill_staging_gate.py passes it. A run
# that never started is not a sample: recording it as a false failure would let the 30-minute
# tick reset the measured streak to zero every time the two collided, which is the metric
# destroying itself rather than measuring anything.
GATE_BUSY = 76

# What --report-busy returns instead of NO_VERDICT. Deliberately NOT returned by default:
# deploy_logic.staging_verdict_summary reads any non-zero that is not 2 as REJECTED, so a third
# code reaching the deployer would report a busy lock as staging rejecting the change.
NOT_RUN = 3

# The restricted key's dispatcher refusing the request outright — a malformed operation name,
# a SHA that is not a 40-hex object name, tags outside its charset. Defined in
# roles/setup/hypervisor/templates/staging-gate-dispatch.sh.j2 and pinned to this constant by
# ansible/tests/staging/test_staging_gate_dispatch.py.
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

# The gate's own ssh identity, and the public half the far side authorizes. Deployed by
# roles/setup/gitops_deploy (private, 0600 here) and roles/setup/hypervisor (public, pinned to a
# forced command there). `identity_problem()` refuses to connect unless these two agree.
IDENTITY = Path("/etc/gitops-deploy/staging_gate_ed25519")
AUTHORIZED_PUBKEY = ROLES / "setup/hypervisor/files/staging-gate.pub"

# The request the dispatcher accepts. Its shape is the interface: an operation name, a full
# 40-hex object name, and the tags — never a script body, because a forced command does not stop
# ssh forwarding stdin.
GATE_OPERATION = "gate"

# Local refusals, both NO_VERDICT. They are distinct from the remote's codes because they mean
# the request never left this host.
IDENTITY_UNUSABLE = 72

# `command not found` on the far side, from either of two causes: the restricted key did not
# authenticate, so a login shell tried to run the literal request string; or the dispatcher ran
# and its exec target was missing. Both mean the gate could not be asked. It is NOT a reliable
# signal about the identity on its own — see the message at the bottom of consult().
SHELL_FALLBACK = 127

# Everything that means "the gate could not be asked". Two of these are local refusals that never
# reach the network, and SHELL_FALLBACK means the request reached a shell instead of the forced
# command — a security regression, which must still not read as staging rejecting the change.
NO_VERDICT_CODES = frozenset(
    {
        PREP_FAILED,
        DISPATCH_REFUSED,
        SSH_FAILURE,
        IDENTITY_UNUSABLE,
        SHELL_FALLBACK,
        GATE_BUSY,
    }
)

# The subset of the above that means the run never STARTED, as opposed to started and could not
# finish. deploy.sh's own 75 is the git-tree lock, which is the same situation one lock down.
BUSY_CODES = frozenset({GATE_BUSY, 75})

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


def classify(rc: int, report_busy: bool = False) -> int:
    """Map the remote script's exit code to a verdict.

    Pure, so the rejecting half of the test suite can drive the same function the runner drives
    instead of asserting arithmetic of its own.

    `report_busy` is off for every caller but the backfill harness, and the default is what the
    deployer gets: a busy lock is NO_VERDICT, indistinguishable from any other reason the gate
    could not be asked. Only a caller measuring the gate needs the distinction.
    """
    if rc == PASS:
        return PASS
    if report_busy and rc in BUSY_CODES:
        return NOT_RUN
    if rc in NO_VERDICT_CODES or rc in DEPLOY_SH_NO_VERDICT:
        return NO_VERDICT
    return REJECTED


def verdict_name(verdict: int) -> str:
    return {
        PASS: "PASS",
        REJECTED: "REJECTED",
        NO_VERDICT: "NO_VERDICT",
        NOT_RUN: "NOT_RUN",
    }[verdict]


def identity_problem() -> str | None:
    """Why the restricted key cannot be used, or None if it is the key the far side authorizes.

    THIS IS THE ANTI-FALLBACK CHECK, and it is the whole reason the switch to the restricted key
    is safe. `IdentitiesOnly=yes` does NOT guarantee our key is the one offered: the default
    identity files still count as configured. So if this key were missing or unloadable, ssh
    would quietly authenticate as the operator instead and the gate would keep working — hiding
    exactly the breakage the switch exists to expose, and re-opening M-3 with nobody noticing.

    A gate that silently regains a full shell is worse than a gate that stops, so this refuses to
    connect at all rather than letting ssh choose. Measured 2026-08-29: a key one byte short of
    its trailing newline fails to load and produces precisely that silent fallback.
    """
    if not IDENTITY.exists():
        return (
            f"{IDENTITY} does not exist — run initial_setup.yml --tags gitops_deploy on this "
            f"host. Refusing to connect, because ssh would fall back to the operator's key and "
            f"the gate would appear to work while running unrestricted."
        )
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(IDENTITY)],
        capture_output=True,
        text=True,
        check=False,
    )
    if derived.returncode != 0:
        return (
            f"{IDENTITY} does not load ({derived.stderr.strip()}). A key missing its trailing "
            f"newline fails exactly this way. Refusing to connect rather than falling back to "
            f"the operator's key."
        )
    if not AUTHORIZED_PUBKEY.exists():
        return f"{AUTHORIZED_PUBKEY} is missing, so the identity cannot be checked"
    if derived.stdout.split()[:2] != AUTHORIZED_PUBKEY.read_text().split()[:2]:
        return (
            f"{IDENTITY} loads but is not the key {AUTHORIZED_PUBKEY} authorizes. Connecting "
            f"would authenticate as something else."
        )
    return None


def run_gate(sha: str, tags: str, timeout: float) -> int:
    """Ask the gate about `sha` over ssh and return the remote's raw exit code."""
    # The dispatcher refuses anything but a full 40-hex object name, and it is right to. Catching
    # it here turns a remote refusal into a local error naming the actual problem.
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        print(
            f"staging-gate: {sha!r} is not a 40-character object name; the gate's forced command "
            f"will refuse it. Pass a full sha, not an abbreviation or a ref.",
            file=sys.stderr,
        )
        return DISPATCH_REFUSED

    problem = identity_problem()
    if problem is not None:
        print(f"staging-gate: {problem}", file=sys.stderr)
        return IDENTITY_UNUSABLE

    try:
        completed = subprocess.run(
            # ServerAlive* is the fix for M-5, and it is not cosmetic. When the transport wedges,
            # the local timeout below kills ssh here — but the remote command is a different
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
                # Bounds which keys are offered. NOT sufficient on its own — the default
                # identity files still count as configured, which is why identity_problem()
                # above is what actually stops a fallback.
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "PreferredAuthentications=publickey",
                "-o",
                "BatchMode=yes",
                "-i",
                str(IDENTITY),
                REMOTE_HOST,
                # The whole request, as ONE argument. It reaches the far side in
                # $SSH_ORIGINAL_COMMAND, where the forced command parses it into an operation
                # name and arguments. Nothing is sent on stdin — see stdin=DEVNULL below.
                f"{GATE_OPERATION} {sha} {tags}",
            ],
            stdin=subprocess.DEVNULL,
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

    if completed.returncode == SHELL_FALLBACK:
        # 127 has TWO causes and this message used to assert the first one. Measured 2026-08-30
        # it was the second: the key authenticated, the forced command ran, and the dispatcher's
        # exec target was missing — and the message sent an operator to audit authorized_keys.
        # Name both, and give the one command that tells them apart.
        print(
            f"staging-gate: the far side exited {SHELL_FALLBACK} (command not found), which is "
            f"NOT a verdict. Either the dispatcher's exec target is missing on daniel-server, "
            f"or the restricted key did not authenticate and a login shell tried to run the "
            f"request as a command. `ssh -v -i {IDENTITY} daniel-server true` tells them apart: "
            f"a 'key options: command' line means the key and its forced command are fine, so "
            f"the exec target is what to look at.",
            file=sys.stderr,
        )
    return completed.returncode


def main() -> int:
    """Deploy `sha` to staging over the one restricted SSH call, and print the verdict.

    Exits PASS (0), REJECTED (1), NO_VERDICT (2), or — with `--report-busy` — NOT_RUN (3)
    when the run never started because the remote lock was held.
    """
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
    parser.add_argument(
        "--report-busy",
        action="store_true",
        help=f"exit {NOT_RUN} when the run never started because a lock was held, instead of "
        f"folding it into NO_VERDICT. For callers that MEASURE the gate; the deployer must "
        f"not pass this.",
    )
    args = parser.parse_args()

    rc = run_gate(args.sha, args.tags, args.timeout)
    verdict = classify(rc, report_busy=args.report_busy)
    print(
        f"staging-gate: {verdict_name(verdict)} (remote exit {rc}) for {args.sha[:8]}"
    )
    return verdict


if __name__ == "__main__":
    sys.exit(main())
