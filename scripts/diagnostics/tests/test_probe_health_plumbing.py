"""`probe.py health coredns|longhorn`: the plumbing checks H1 moves off the host (#5923, #5924).

Each check reads through an injected `read` (kubectl's `-o json` document, or None for a
NotFound) and, for CoreDNS, an injected resolver, so no case needs a cluster or a dig binary.
The fixtures copy the live objects read from daniel-box on 2026-10-22.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_plumbing.py
"""

import subprocess

import pytest

from deploy_tools.deploy_lib import detach_notify
from diagnostics.probe_lib import core, health, health_alerts, health_plumbing

_QUIET = health_alerts.PromTools(query=lambda promql: [], sleep=lambda s: None)

_NODEHOSTS = "192.168.1.10 daniel-box\n192.168.1.11 daniel-server\n"
_KUBE_DNS = {"spec": {"clusterIP": "10.43.0.10"}}

_LIVE_PARAMS = {
    "backupTargetName": "default",
    "dataEngine": "v1",
    "dataLocality": "disabled",
    "disableRevisionCounter": "true",
    "fromBackup": "",
    "fsType": "ext4",
    "staleReplicaTimeout": "30",
    "unmapMarkSnapChainRemoved": "ignored",
}
# The ConfigMap key opens with a comment block and `---`, as the live one does.
_CM_YAML = """---
# Longhorn's default StorageClass.
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: longhorn
provisioner: driver.longhorn.io
parameters:
  staleReplicaTimeout: "30"
  fromBackup: ""
  fsType: "ext4"
  dataLocality: "disabled"
  unmapMarkSnapChainRemoved: "ignored"
  disableRevisionCounter: "true"
  dataEngine: "v1"
  backupTargetName: "default"
"""


class _Reader:
    """A `read` that answers from {(resource, name): doc}, None for anything else."""

    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def __call__(self, args):
        self.calls.append(args)
        return self.objects.get((args[1], args[2]))


_PROD_NODES = {"items": [{"metadata": {"name": "daniel-box"}}]}


def _box():
    return "daniel-box"


def _coredns_objects(nodehosts=_NODEHOSTS):
    return {
        ("nodes", "-o"): _PROD_NODES,
        ("configmap", "coredns"): {
            "data": {"Corefile": ".:53 {}", "NodeHosts": nodehosts}
        },
        ("service", "kube-dns"): _KUBE_DNS,
    }


class _Answers:
    """A resolver that answers from {name: [answer]}, [] for anything else."""

    def __init__(self, **by_name):
        self.by_name, self.asked = by_name, []

    def __call__(self, server, name):
        self.asked.append((server, name))
        return self.by_name.get(name, [])


_RESOLVING = {
    health_plumbing.CLUSTER_NAME: ["10.43.0.1"],
    health_plumbing.UPSTREAM_NAME: ["10.42.0.136"],
}


def _longhorn_objects(live_params=_LIVE_PARAMS, cm_yaml=_CM_YAML):
    return {
        ("storageclass", "longhorn"): {"parameters": dict(live_params)},
        ("configmap", "longhorn-storageclass"): {
            "data": {"storageclass.yaml": cm_yaml}
        },
    }


def test_the_check_table_names_coredns_and_longhorn():
    assert {"coredns", "longhorn"} <= set(health_plumbing.CHECKS)


def test_coredns_nodehosts_is_clean():
    failures, notes = health_plumbing.coredns(
        _Reader(_coredns_objects()), _Answers(**_RESOLVING), _box
    )
    assert failures == []
    assert "NodeHosts lists 2 hosts" in notes


def test_coredns_nodehosts_is_flagged():
    failures, _ = health_plumbing.coredns(
        _Reader(_coredns_objects(nodehosts="\n")), _Answers(**_RESOLVING), _box
    )
    assert len(failures) == 1
    assert "empty NodeHosts" in failures[0]


def test_coredns_resolution_is_clean():
    resolve = _Answers(**_RESOLVING)
    failures, _ = health_plumbing.coredns(_Reader(_coredns_objects()), resolve, _box)
    assert failures == []
    assert resolve.asked == [
        ("10.43.0.10", health_plumbing.CLUSTER_NAME),
        ("10.43.0.10", health_plumbing.UPSTREAM_NAME),
    ]


def test_coredns_resolution_is_flagged():
    """A Corefile that loads but no longer forwards to Pi-hole answers the cluster name only."""
    resolve = _Answers(**{health_plumbing.CLUSTER_NAME: ["10.43.0.1"]})
    failures, _ = health_plumbing.coredns(_Reader(_coredns_objects()), resolve, _box)
    assert failures == [
        f"{health_plumbing.UPSTREAM_NAME} did not resolve through kube-dns 10.43.0.10 "
        "— the Corefile loads but does not answer it"
    ]


def test_coredns_without_the_kube_dns_service_is_flagged():
    objects = _coredns_objects()
    del objects[("service", "kube-dns")]
    resolve = _Answers(**_RESOLVING)
    failures, _ = health_plumbing.coredns(_Reader(objects), resolve, _box)
    assert "Service kube-system/kube-dns is MISSING" in failures[0]
    assert resolve.asked == []


def test_a_node_of_the_served_cluster_is_clean():
    health_plumbing.require_a_node_of_the_served_cluster(
        _Reader(_coredns_objects()), _box
    )


@pytest.mark.parametrize("host", ["daniel-stage", "laptop"])
def test_a_host_outside_the_served_cluster_is_flagged(host):
    """daniel-stage would dig another cluster's CoreDNS, and a laptop cannot route a ClusterIP."""
    resolve = _Answers(**_RESOLVING)
    with pytest.raises(core.Unreadable, match=f"this host \\({host}\\)"):
        health_plumbing.coredns(_Reader(_coredns_objects()), resolve, lambda: host)
    assert resolve.asked == []


def test_longhorn_storageclass_is_clean():
    failures, notes = health_plumbing.longhorn_storageclass(
        _Reader(_longhorn_objects())
    )
    assert failures == []
    assert notes == [
        "StorageClass longhorn matches longhorn-system/longhorn-storageclass (8 parameters)"
    ]


def test_longhorn_storageclass_is_flagged():
    drifted = {**_LIVE_PARAMS, "dataLocality": "best-effort"}
    failures, _ = health_plumbing.longhorn_storageclass(
        _Reader(_longhorn_objects(live_params=drifted))
    )
    assert len(failures) == 1
    assert "dataLocality: live=best-effort, ConfigMap=disabled" in failures[0]
    assert "operator-only `kubectl delete storageclass longhorn`" in failures[0]


def test_a_parameter_absent_from_the_live_class_is_flagged():
    """`fromBackup: ""` is declared, so a live class without the key has drifted."""
    live = {k: v for k, v in _LIVE_PARAMS.items() if k != "fromBackup"}
    failures, _ = health_plumbing.longhorn_storageclass(
        _Reader(_longhorn_objects(live_params=live))
    )
    assert "fromBackup: live=<absent>, ConfigMap=" in failures[0]


def test_a_configmap_with_no_storageclass_is_flagged():
    failures, _ = health_plumbing.longhorn_storageclass(
        _Reader(_longhorn_objects(cm_yaml="kind: ConfigMap\n"))
    )
    assert failures == [
        "ConfigMap longhorn-system/longhorn-storageclass's storageclass.yaml key holds "
        "no StorageClass"
    ]


def _dig_run(returncode, stdout=""):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "dig: error")

    return run


def _which(name):
    return "/usr/bin/dig"


def test_dig_reads_the_answer_lines():
    run = _dig_run(0, ";; a comment line\n10.43.0.1\n")
    assert health_plumbing.dig("10.43.0.10", "x", run=run, which=_which) == [
        "10.43.0.1"
    ]


@pytest.mark.parametrize("returncode", [0, health_plumbing.DIG_NO_REPLY])
def test_no_answer_and_no_reply_both_read_as_unresolved(returncode):
    stdout = ";; connection timed out; no servers could be reached\n"
    run = _dig_run(returncode, stdout if returncode else "")
    assert health_plumbing.dig("10.43.0.10", "x", run=run, which=_which) == []


def test_a_dig_that_cannot_run_is_unreadable():
    with pytest.raises(core.Unreadable):
        health_plumbing.dig("10.43.0.10", "x", run=_dig_run(0), which=lambda n: None)
    with pytest.raises(core.Unreadable):
        health_plumbing.dig("10.43.0.10", "x", run=_dig_run(10), which=_which)


def test_a_failing_check_leads_the_gate_verdict():
    """The notifier reads only line 1, so the failure must be there, and must not read as a skip."""
    drifted = {**_LIVE_PARAMS, "fsType": "xfs"}
    read = _Reader(_longhorn_objects(live_params=drifted))
    text, code = health._with_alert_plane(
        "longhorn", "longhorn: all 1 workload healthy", 0, None, _QUIET, read
    )
    head = text.splitlines()[0]
    assert code == 1
    assert (
        head == "longhorn: the plumbing check FAILED the gate after a healthy rollout"
    )
    assert not any(m in head for m in detach_notify.NOT_APPLICABLE_MARKERS)


def test_a_failed_rollout_reads_no_plumbing():
    read = _Reader(_longhorn_objects())
    assert health._with_alert_plane(
        "longhorn", "longhorn: 1 of 1 workloads FAILED", 1, None, _QUIET, read
    ) == ("longhorn: 1 of 1 workloads FAILED", 1)
    assert read.calls == []


def test_a_tag_with_no_check_reads_nothing():
    read = _Reader({})
    assert health_plumbing.with_checks("jellyfin", "ok", read) == ("ok", 0)
    assert read.calls == []
