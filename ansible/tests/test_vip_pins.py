#!/usr/bin/env python3
"""Every VIP-backed workload must be pinned to the node that announces the VIP.

WHY THIS IS A TEST AND NOT A COMMENT. MetalLB's L2Advertisement announces from
daniel-box alone (roles/setup/k3s/templates/metallb-pool.yaml.j2 documents why that is
permanent). With `externalTrafficPolicy: Local`, the announcing node forwards a packet
only to a backend on *itself* — so a VIP-backed pod scheduled onto daniel-server makes
its own service black-hole every packet.

The failure is silent in the worst way: the pod passes its probes, `kubectl get pods`
shows Running, and only traffic from the LAN disappears. It has already happened twice
in production here (the 2026-08-14 post-boot DNS blackout, and the node-join blackout
before it), and on 2026-08-14 an audit found two more workloads — valheim and wg-easy —
that had been carrying a VIP with no pin since the day they went live, surviving purely
because the scheduler happened to place them correctly.

A comment cannot catch the next one. This can: add a `type: LoadBalancer` service with
ETP Local and no pin, and the suite fails.

Run: uv run pytest ansible/tests/test_vip_pins.py
"""

import re
from pathlib import Path

import pytest

K8S_ROLES = Path(__file__).resolve().parents[1] / "roles" / "k8s"

ANNOUNCING_NODE = "daniel-box"

# jellyfin is pinned by storage, not by its own nodeSelector: the media-volume `local` PV
# declares a required nodeAffinity on the node it lives on, which the scheduler enforces
# just as hard. Listed explicitly rather than special-cased in the logic so that moving the
# media volume off node-local storage forces someone to revisit this line.
PINNED_BY_VOLUME = {"jellyfin"}


def _workload_templates(role_dir):
    for tpl in sorted(role_dir.glob("templates/*.j2")):
        text = tpl.read_text()
        if re.search(r"^kind: (Deployment|DaemonSet|StatefulSet)", text, re.M):
            yield tpl, text


def _vip_service_roles():
    """Roles owning a Service that is both type: LoadBalancer and ETP Local."""
    found = []
    for role_dir in sorted(K8S_ROLES.iterdir()):
        if not role_dir.is_dir():
            continue
        for tpl in sorted(role_dir.glob("templates/*.j2")):
            text = tpl.read_text()
            if "type: LoadBalancer" in text and "externalTrafficPolicy: Local" in text:
                found.append((role_dir.name, tpl.name))
                break
    return found


def test_some_vip_services_exist():
    """Guard against the discovery logic silently matching nothing."""
    assert _vip_service_roles(), (
        "found no LoadBalancer + ETP Local services — check the matcher"
    )


@pytest.mark.parametrize("role,svc_template", _vip_service_roles())
def test_vip_backed_workload_is_pinned_to_the_announcing_node(role, svc_template):
    role_dir = K8S_ROLES / role
    if role in PINNED_BY_VOLUME:
        pytest.skip(
            f"{role} is pinned by the media-volume local PV's required nodeAffinity"
        )

    workloads = list(_workload_templates(role_dir))
    assert workloads, f"{role} has {svc_template} but no workload template to pin"

    for tpl, text in workloads:
        if re.search(r"^kind: DaemonSet", text, re.M):
            continue  # already runs on every node, including the announcer
        assert "nodeSelector:" in text, (
            f"{role}/{tpl.name} backs a LoadBalancer service with externalTrafficPolicy: "
            f"Local ({svc_template}) but has no nodeSelector. MetalLB announces only from "
            f"{ANNOUNCING_NODE}, so if this pod lands on the other node its VIP drops every "
            f"packet while the pod still reports healthy."
        )
        assert re.search(
            rf"kubernetes\.io/hostname:\s*{re.escape(ANNOUNCING_NODE)}", text
        ), (
            f"{role}/{tpl.name} is pinned, but not to {ANNOUNCING_NODE} — the node the "
            f"MetalLB L2Advertisement announces from. Announcement and placement move as "
            f"one unit; see roles/setup/k3s/templates/metallb-pool.yaml.j2."
        )
