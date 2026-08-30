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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe_health as ph  # noqa: E402


def _svc(name, etp="Local", ip="10.0.0.240", type_="LoadBalancer"):
    return {
        "metadata": {"namespace": "homelab", "name": name},
        "spec": {"type": type_, "externalTrafficPolicy": etp},
        "status": {"loadBalancer": {"ingress": [{"ip": ip}]}},
    }


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


def test_the_probe_only_reads():
    """Every call is a `get`. This runs against a cluster Ansible alone may write."""
    for argv in ph.vip_placement_argv():
        assert argv[:2] == ["kubectl", "get"]
