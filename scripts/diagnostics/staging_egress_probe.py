#!/usr/bin/env python3
"""Acceptance gate for the staging guest's egress fence.

The staging cluster's whole premise is that it cannot hurt production
(docs/staging-cluster.md, Decision 2). Its libvirt network is `<forward mode='nat'/>`, which
carries no destination constraint, so until the egress fence existed the guest reached the
entire production LAN — masqueraded as daniel-server, a trusted node. Measured from inside the
guest on 2026-08-27: the MetalLB VIP answered 301, the k3s API answered 401, and daniel-pi's
unauthenticated wg-easy admin UI answered 200.

It also reached the CLUSTER network, which the first fence did not cover and which no LAN rule
can. Measured from inside the guest on 2026-08-28: prod's Longhorn API answered 200 on its
ClusterIP with a mutating `diskUpdate` action in the payload, and a longhorn-ui pod IP answered
200 directly. Traffic to a production ClusterIP is DNATed by kube-proxy on daniel-server as it
is forwarded, so a guest with a default route through that host lands inside the cluster
network without any of it being on the LAN.

WHY THIS RUNS INSIDE THE GUEST. A firewall rule that is present and INERT reads exactly like a
working one from the host — `ufw status` and `virsh nwfilter-dumpxml` list it either way, and
whether it is attached to a running interface is not visible in that output. Reachability from
the far side is the only signal that separates the two, which is why this is the acceptance
gate and the pytest guard beside it only checks the rule's shape.

WHY THE CONTROL TARGET IS LOAD-BEARING. A fence that severed the guest's internet as well would
also make every production target fail, and the run would read as a pass. The control target
must stay reachable for the result to mean anything, so a lost control is reported as a FAILURE
of the fence's design rather than a success of its blocking.

WHY THE CLUSTER TARGETS CARRY A HOST-SIDE CONTROL LEG. Their addresses are allocated, not
pinned — a pod IP is ephemeral and Longhorn's ClusterIP is whatever the API server handed it.
A target that has simply moved answers nothing from anywhere, which is indistinguishable from a
fence that works. So each cluster target is dialled from daniel-server too: unreachable from
BOTH sides means the target is stale, and that exits 2 rather than 0. Without this leg the
probe would pass forever on an address production had moved off.

Run it on daniel-server, which is the only host that can route to the guest AND the host whose
kube-proxy rules make the cluster targets reachable at all:

    uv run python scripts/diagnostics/staging_egress_probe.py

Exit 0 the fence holds; 1 the fence is broken or inert; 2 the probe could not be run at all,
or a target is stale. That third code matters — an unreachable guest is not a passing fence,
and collapsing it into 1 would make a rebuilt VM look like a security regression.
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

# Longhorn's frontend Service, the thing actually found exposed on 2026-08-28. Its ClusterIP is
# allocated rather than pinned in a manifest, so it is written here as a probe target and NOT
# as inventory: nothing deploys from this value, and the host-side control leg turns a stale
# one into exit 2. Do not swap it for k8s_registry_cluster_ip or dns_k8s_cluster_ip — both are
# pinned and both already answer nothing from the guest, so either would read as a held fence
# forever and could never go red.
LONGHORN_PROBE_URL = "http://10.43.152.244/v1/nodes"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _pod_ip_on_this_host() -> str | None:
    """A live pod IP on daniel-server's own CNI bridge, read from the neighbour table.

    Discovered rather than pinned because pod IPs are ephemeral, and read from `ip neigh`
    rather than kubectl because daniel-server is an agent node with no kubeconfig — kubectl
    there dials localhost:8080 and is refused.
    """
    try:
        done = subprocess.run(
            ["ip", "-4", "neigh", "show", "dev", "cni0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    for line in done.stdout.splitlines():
        fields = line.split()
        if fields and "REACHABLE" in fields:
            return fields[0]
    return None


def _curl(url: str) -> str:
    return f"curl -sk --max-time {CURL_TIMEOUT} -o /dev/null {url}"


def _targets() -> tuple[str, list[tuple[str, str, str | None]]]:
    """The guest's address, and the (label, guest command, host control command) triples.

    The LAN-side addresses are read out of the inventory rather than written here. Three of
    them are production addresses, and a probe that kept testing an address production had
    moved off would report a fence that no longer fences anything.

    A `None` control command means the target is pinned in inventory and cannot go stale
    without a commit, so there is nothing for a second dial to prove.
    """
    all_vars = _load(INVENTORY / "group_vars" / "all.yml")
    box = _load(INVENTORY / "host_vars" / "daniel-box.yml")
    pi = _load(INVENTORY / "host_vars" / "daniel-pi.yml")
    hypervisor = _load(
        REPO / "ansible" / "roles" / "setup" / "hypervisor" / "defaults" / "main.yml"
    )

    guest = all_vars["staging_vm_ip"]
    gateway = hypervisor["hypervisor_staging_net_gateway"]

    targets: list[tuple[str, str, str | None]] = [
        # The control. Not a production address — it proves the guest still has egress at all.
        ("INTERNET", _curl("https://1.1.1.1"), None),
        ("PRODVIP", _curl(f"http://{all_vars['k3s_metallb_ingress_vip']}"), None),
        ("K3SAPI", _curl(f"https://{box['server_ip']}:6443/version"), None),
        # wg-easy's admin UI. Unauthenticated, and its only protection is being LAN-only —
        # the premise the guest sat inside before the fence.
        ("WGEASY", _curl(f"http://{pi['server_ip']}:51821"), None),
        # The two below reach daniel-server ITSELF rather than through it, so they are INPUT
        # and the routed fence does not cover them. They are here because that is exactly why
        # they are easy to forget: the guest can dial the hypervisor's own kubelet, on the
        # staging bridge address as readily as on the LAN one, and land inside the production
        # cluster without a single packet being forwarded. Measured blocked on 2026-08-27
        # BEFORE the fence existed — ufw's pre-existing `deny incoming` default already
        # covers them. So these are a regression guard on that default, not a second fence,
        # and if they ever flip the fix is an INPUT rule, not a wider `route deny`.
        ("HOSTKUBELET", _curl(f"https://{gateway}:10250/healthz"), None),
        (
            "LANKUBELET",
            _curl(
                f"https://{_load(INVENTORY / 'host_vars' / 'daniel-server.yml')['server_ip']}:10250/healthz"
            ),
            None,
        ),
        # The Service CIDR. This is the finding the LAN-only fence missed.
        ("LONGHORNSVC", _curl(LONGHORN_PROBE_URL), _curl(LONGHORN_PROBE_URL)),
    ]

    # The pod CIDR. ICMP rather than HTTP because the discovered neighbour is whichever pod
    # happens to be up, and its listening port is unknown; the nwfilter rule carries no
    # protocol, so a ping proves the same rule an HTTP dial would.
    pod_ip = _pod_ip_on_this_host()
    if pod_ip:
        ping = f"ping -c1 -W3 {pod_ip}"
        targets.append(("PODNET", ping, ping))
    return guest, targets


def _run(cmd: list[str]) -> bool | None:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except OSError, subprocess.TimeoutExpired:
        return None
    # 255 is ssh's own failure code, so the guest was never reached and the probe never ran.
    if done.returncode == 255:
        return None
    return done.returncode == 0


def _reachable(guest: str, command: str) -> bool | None:
    """True reachable, False refused, None the probe itself could not run."""
    return _run(["ssh", *SSH_OPTS, f"ubuntu@{guest}", command])


def _reachable_from_here(command: str) -> bool | None:
    """The control leg: the same dial from daniel-server, where it must still work."""
    return _run(["bash", "-c", command])


def main() -> int:
    guest, targets = _targets()
    results: dict[str, bool | None] = {}
    controls: dict[str, str] = {}

    for label, command, control in targets:
        results[label] = _reachable(guest, command)
        if control:
            controls[label] = control
        state = {
            True: "REACHABLE",
            False: "blocked",
            None: "UNREACHABLE (probe failed)",
        }[results[label]]
        print(f"{label:<12} {state:<26} {command}")

    if any(v is None for v in results.values()):
        print(
            f"\nCould not reach the guest at {guest} over ssh, so nothing was proven. This is "
            f"NOT a passing fence — run it on daniel-server, the only host that routes to the "
            f"staging network."
        )
        return 2

    # A cluster target that answers from neither side has moved, and a moved target proves
    # nothing. Checked before the leak verdict so a stale address can never read as a pass.
    stale = sorted(
        label
        for label, command in controls.items()
        if not results[label] and not _reachable_from_here(command)
    )
    if stale:
        print(
            f"\nTARGET STALE: {stale} answered from neither the guest nor daniel-server, so "
            f"the fence was not tested against them. Their addresses are allocated, not "
            f"pinned — find where the workload moved and update the probe. This is not a "
            f"passing fence."
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
            f"foreign. Check that the guest's live interface carries the fence — a filter that "
            f"is defined but unreferenced reads identical to a working one:\n"
            f"    virsh -c qemu:///system dumpxml daniel-stage | grep -A2 filterref\n"
            f"    virsh -c qemu:///system nwfilter-dumpxml staging-egress-fence"
        )
        return 1

    print(
        "\nFence holds: the guest keeps its internet egress and reaches no production target."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
