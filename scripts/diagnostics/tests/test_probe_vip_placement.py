"""Tests for `probe.py vip-placement`.

The probe asserts that every `externalTrafficPolicy: Local` MetalLB VIP has a Ready endpoint
on the node that announces it. When it does not, kube-proxy installs a filter-table
KUBE-EXTERNAL-SERVICES DROP for that VIP on the announcer and every forwarded LAN packet dies
there — while the Service reads Ready and the pod reads 1/1.

The reject cases are not synthetic. `test_the_cold_boot_reschedule_is_the_red` replays the
2026-08-14 outage exactly: the L2Advertisement stayed pinned to daniel-box while a cold boot
rescheduled pihole onto daniel-server, and LAN DNS went down for everyone.

`test_an_empty_read_is_inconclusive_not_a_pass` is the control. Every other check here reads
lists, and a probe over an empty list reports "all zero are fine" — the
`an-optimisation-can-land-green-and-be-inert` shape.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import vip_placement as ph


def _svc(name, etp="Local", ip="10.0.0.240", type_="LoadBalancer", selector=None):
    return {
        "metadata": {"namespace": "homelab", "name": name},
        "spec": {
            "type": type_,
            "externalTrafficPolicy": etp,
            "selector": {"app": name} if selector is None else selector,
        },
        "status": {"loadBalancer": {"ingress": [{"ip": ip}]}},
    }


def _workload(app, replicas, namespace="homelab"):
    """A Deployment as `kubectl get -o json` returns it. `replicas=None` omits the field."""
    spec = {
        "template": {
            "metadata": {"labels": {"app": app, "netpol-baseline": "enforced"}}
        }
    }
    if replicas is not None:
        spec["replicas"] = replicas
    return {"metadata": {"namespace": namespace, "name": app}, "spec": spec}


def _slice(service_name, node, ready=True):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service_name}},
        "endpoints": [{"nodeName": node, "conditions": {"ready": ready}}],
    }


def _node(name):
    return {"metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}}}


_BOX = {"daniel-box"}


def test_a_vip_backed_on_the_announcing_node_passes():
    text, code = ph.format_vip_placement(
        [_svc("traefik")], [_slice("traefik", "daniel-box")], _BOX
    )
    assert code == 0 and "OK:" in text


def test_the_cold_boot_reschedule_is_the_red():
    """The 2026-08-14 outage, replayed.

    The announcer stayed pinned to daniel-box; the pod moved to daniel-server. Nothing about
    the Service or the pod is unhealthy, which is exactly why this needs its own check.
    """
    text, code = ph.format_vip_placement(
        [_svc("pihole-dns", ip="10.0.0.243")],
        [_slice("pihole-dns", "daniel-server")],
        _BOX,
    )
    assert code == 1
    assert "FAIL" in text and "pihole-dns" in text and "10.0.0.243" in text


def test_an_unready_endpoint_is_not_a_local_endpoint():
    """kube-proxy's rule counts Ready endpoints. Counting an unready one would hide the DROP."""
    _text, code = ph.format_vip_placement(
        [_svc("traefik")], [_slice("traefik", "daniel-box", ready=False)], _BOX
    )
    assert code == 1


def test_an_empty_read_is_inconclusive_not_a_pass():
    """No Services read: the check has nothing to assert and must not report OK."""
    text, code = ph.format_vip_placement([], [_slice("traefik", "daniel-box")], _BOX)
    assert code == 2 and "INCONCLUSIVE" in text


def test_no_announcer_resolved_is_inconclusive_not_a_pass():
    """The other half of the control: without an announcer, every VIP looks stranded."""
    text, code = ph.format_vip_placement(
        [_svc("traefik")], [_slice("traefik", "daniel-box")], set()
    )
    assert code == 2 and "INCONCLUSIVE" in text


def test_a_cluster_policy_service_is_not_checked():
    """`externalTrafficPolicy: Cluster` has no placement rule — flagging it would be noise."""
    text, code = ph.format_vip_placement(
        [_svc("traefik", etp="Cluster")], [_slice("traefik", "daniel-server")], _BOX
    )
    assert code == 2, "nothing in scope, so nothing to assert"
    assert "INCONCLUSIVE" in text


def test_a_clusterip_service_is_not_checked():
    _text, code = ph.format_vip_placement(
        [_svc("api", type_="ClusterIP")], [_slice("api", "daniel-server")], _BOX
    )
    assert code == 2


def test_an_unpinned_advertisement_announces_from_every_node():
    nodes = [_node("daniel-box"), _node("daniel-server")]
    assert ph.announcing_nodes([{"spec": {}}], nodes) == {
        "daniel-box",
        "daniel-server",
    }


def test_a_hostname_pinned_advertisement_resolves_to_that_node():
    nodes = [_node("daniel-box"), _node("daniel-server")]
    advert = {
        "spec": {
            "nodeSelectors": [{"matchLabels": {"kubernetes.io/hostname": "daniel-box"}}]
        }
    }
    assert ph.announcing_nodes([advert], nodes) == {"daniel-box"}


def test_a_selector_matching_nothing_resolves_to_nothing():
    """Which the formatter turns into INCONCLUSIVE, not a silent all-stranded FAIL."""
    advert = {
        "spec": {"nodeSelectors": [{"matchLabels": {"kubernetes.io/hostname": "gone"}}]}
    }
    assert ph.announcing_nodes([advert], [_node("daniel-box")]) == set()


def test_a_zero_replica_workload_is_not_stranded():
    """terraria is parked at zero replicas by design, so its empty endpoint set is expected.

    Issue #870: this FAILed the whole command, and a check that is red whenever a service is
    intentionally off trains its reader to ignore the red.
    """
    text, code = ph.format_vip_placement(
        [_svc("terraria", ip="10.0.0.245")], [], _BOX, [_workload("terraria", 0)]
    )
    assert code == 0
    assert "scaled-to-zero" in text, "the row stays visible rather than being omitted"
    assert "terraria" in text and "not checked" in text


def test_a_one_replica_workload_with_no_endpoint_is_still_flagged():
    """The reject half. Without it the skip could match every workload."""
    _text, code = ph.format_vip_placement(
        [_svc("pihole-dns")], [], _BOX, [_workload("pihole-dns", 1)]
    )
    assert code == 1


def test_an_absent_replicas_field_is_not_zero():
    """A missing `spec.replicas` means the API default of 1, so a falsy test would fail open."""
    _text, code = ph.format_vip_placement(
        [_svc("pihole-dns")], [], _BOX, [_workload("pihole-dns", None)]
    )
    assert code == 1


def test_a_selector_resolving_to_no_workload_is_still_flagged():
    """A Service pointing at nothing is the class of bug this probe exists for."""
    _text, code = ph.format_vip_placement([_svc("pihole-dns")], [], _BOX, [])
    assert code == 1


def test_one_of_two_matching_workloads_at_zero_is_still_flagged():
    """Off means EVERY matching workload declares zero, not just one of them."""
    _text, code = ph.format_vip_placement(
        [_svc("pihole-dns")],
        [],
        _BOX,
        [_workload("pihole-dns", 0), _workload("pihole-dns", 1)],
    )
    assert code == 1


def test_an_empty_service_selector_matches_no_workload():
    """`all()` over an empty selector is True, which would match every workload in scope."""
    assert ph.workload_replicas([_workload("terraria", 0)], "homelab", {}) == []


def test_a_workload_in_another_namespace_does_not_match():
    assert (
        ph.workload_replicas(
            [_workload("terraria", 0, namespace="observability")],
            "homelab",
            {"app": "terraria"},
        )
        == []
    )


def test_a_scaled_to_zero_vip_with_an_endpoint_still_reads_ok():
    """A backed VIP is `ok` on its endpoints, never reclassified by its replica count."""
    text, code = ph.format_vip_placement(
        [_svc("terraria")],
        [_slice("terraria", "daniel-box")],
        _BOX,
        [_workload("terraria", 0)],
    )
    assert code == 0 and "scaled-to-zero" not in text


def test_the_probe_only_reads():
    """Every call is a `get`. This runs against a cluster Ansible alone may write."""
    argvs = ph.vip_placement_argv()
    assert {argv[2] for argv in argvs} == {
        "svc",
        "endpointslices",
        "l2advertisements.metallb.io",
        "nodes",
        "deployments,statefulsets",
    }, "a dropped read would leave this loop passing over fewer calls"
    for argv in argvs:
        assert argv[:2] == ["kubectl", "get"]
