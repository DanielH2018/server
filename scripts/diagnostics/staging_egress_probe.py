#!/usr/bin/env python3
"""Acceptance gate for the staging guest's egress fence.

The staging cluster's whole premise is that it cannot hurt production
(docs/staging-cluster.md, Decision 2). Its libvirt network is `<forward mode='nat'/>`, which
carries no destination constraint, so until the ufw `route deny` rule in
`roles/setup/initial_setup/tasks/network.yml` existed the guest reached the entire production
LAN — masqueraded as daniel-server, a trusted node. Measured from inside the guest on
2026-08-27: the MetalLB VIP answered 301, the k3s API answered 401, and daniel-pi's
unauthenticated wg-easy admin UI answered 200.

WHY THIS RUNS INSIDE THE GUEST. A firewall rule that is present and INERT reads exactly like a
working one from the host — `ufw status` lists it either way, and the ordering that decides
whether it fires is not visible in that output. Reachability from the far side is the only
signal that separates the two, which is why this is the acceptance gate and the pytest guard
beside it only checks the rule's shape.

WHY THE CONTROL TARGET IS LOAD-BEARING. A fence that severed the guest's internet as well would
also make every production target fail, and the run would read as a pass. The control target
must stay reachable for the result to mean anything, so a lost control is reported as a FAILURE
of the fence's design rather than a success of its blocking.

Run it on daniel-server, which is the only host that can route to the guest:

    uv run python scripts/diagnostics/staging_egress_probe.py

Exit 0 the fence holds; 1 the fence is broken or inert; 2 the probe could not be run at all.
That third code matters — an unreachable guest is not a passing fence, and collapsing it into
1 would make a rebuilt VM look like a security regression.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "ansible" / "inventory"

SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "StrictHostKeyChecking=accept-new",
]
# Long enough that a slow-but-permitted answer is not read as a block, short enough that four
# refused targets do not stall a deploy gate.
CURL_TIMEOUT = 6


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _targets() -> tuple[str, list[tuple[str, str]]]:
    """The guest's address, and the (label, url) pairs to probe.

    Read out of the inventory rather than written here. Three of these are production
    addresses, and a probe that kept testing an address production had moved off would report
    a fence that no longer fences anything.
    """
    all_vars = _load(INVENTORY / "group_vars" / "all.yml")
    box = _load(INVENTORY / "host_vars" / "daniel-box.yml")
    pi = _load(INVENTORY / "host_vars" / "daniel-pi.yml")
    hypervisor = _load(
        REPO / "ansible" / "roles" / "setup" / "hypervisor" / "defaults" / "main.yml"
    )

    guest = all_vars["staging_vm_ip"]
    gateway = hypervisor["hypervisor_staging_net_gateway"]
    return guest, [
        # The control. Not a production address — it proves the guest still has egress at all.
        ("INTERNET", "https://1.1.1.1"),
        ("PRODVIP", f"http://{all_vars['k3s_metallb_ingress_vip']}"),
        ("K3SAPI", f"https://{box['server_ip']}:6443/version"),
        # wg-easy's admin UI. Unauthenticated, and its only protection is being LAN-only —
        # the premise the guest sat inside before the fence.
        ("WGEASY", f"http://{pi['server_ip']}:51821"),
        # The two below reach daniel-server ITSELF rather than through it, so they are INPUT
        # and the routed fence does not cover them. They are here because that is exactly why
        # they are easy to forget: the guest can dial the hypervisor's own kubelet, on the
        # staging bridge address as readily as on the LAN one, and land inside the production
        # cluster without a single packet being forwarded. Measured blocked on 2026-08-27
        # BEFORE the fence existed — ufw's pre-existing `deny incoming` default already
        # covers them. So these are a regression guard on that default, not a second fence,
        # and if they ever flip the fix is an INPUT rule, not a wider `route deny`.
        ("HOSTKUBELET", f"https://{gateway}:10250/healthz"),
        (
            "LANKUBELET",
            f"https://{_load(INVENTORY / 'host_vars' / 'daniel-server.yml')['server_ip']}:10250/healthz",
        ),
    ]


def _reachable(guest: str, url: str) -> bool | None:
    """True reachable, False refused, None the probe itself could not run."""
    cmd = [
        "ssh",
        *SSH_OPTS,
        f"ubuntu@{guest}",
        f"curl -sk --max-time {CURL_TIMEOUT} -o /dev/null {url}",
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except OSError, subprocess.TimeoutExpired:
        return None
    # 255 is ssh's own failure code, so the guest was never reached and curl never ran.
    if done.returncode == 255:
        return None
    return done.returncode == 0


def main() -> int:
    guest, targets = _targets()
    results: dict[str, bool | None] = {}

    for label, url in targets:
        results[label] = _reachable(guest, url)
        state = {
            True: "REACHABLE",
            False: "blocked",
            None: "UNREACHABLE (probe failed)",
        }[results[label]]
        print(f"{label:<9} {state:<26} {url}")

    if any(v is None for v in results.values()):
        print(
            f"\nCould not reach the guest at {guest} over ssh, so nothing was proven. This is "
            f"NOT a passing fence — run it on daniel-server, the only host that routes to the "
            f"staging network."
        )
        return 2

    control = results.pop("INTERNET")
    leaked = sorted(label for label, ok in results.items() if ok)

    if not control:
        print(
            "\nThe control target is unreachable, so the guest has no egress at all. Every "
            "production target below 'passed' only because nothing can leave — that is a "
            "broken fence, not a working one."
        )
        return 1
    if leaked:
        print(
            f"\nFENCE BROKEN: {leaked} answered from inside the staging guest. Traffic arrives "
            f"at production SNATed as daniel-server, so source-IP controls (authelia's "
            f"lan_subnet bypass rules, wg-easy's LAN-only premise) do not see staging as "
            f"foreign. Check that the ufw route rule in "
            f"roles/setup/initial_setup/tasks/network.yml is present AND ahead of libvirt's "
            f"own accept for this bridge."
        )
        return 1

    print(
        "\nFence holds: the guest keeps its internet egress and reaches no production target."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
