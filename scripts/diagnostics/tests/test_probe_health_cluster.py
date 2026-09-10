"""`probe.py health --cluster`: the gate must be about the cluster it says it is.

Every kubectl argv the gate builds is a bare `k3s kubectl`, so it reads whatever the local
install serves. Run after a `-e target=daniel-stage` deploy on daniel-box it therefore gated
PRODUCTION's workload and exited 0 — a healthy verdict about a cluster the deploy never
touched (#1663). That is worse than no gate: an inert check reports nothing, this one reported
a pass about the wrong subject.

`--cluster` names the intended cluster and `wrong_cluster` refuses when the reachable one is a
different one. The pair below is the point: the prod path must still pass, and the staging
request against a prod kubectl must NOT return OK.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cluster.py
"""

import re

from lib.repo_paths import REPO

from diagnostics.probe_lib import health, health_kubectl

PROD_NODES = {
    "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
}
STAGE_NODES = {"items": [{"metadata": {"name": "daniel-stage"}}]}


def test_nodes_argv_is_the_read_the_gate_makes():
    assert health_kubectl.k8s_nodes_argv() == [
        "k3s",
        "kubectl",
        "get",
        "nodes",
        "-o",
        "json",
    ]


def test_cluster_of_reads_each_clusters_nodes():
    assert health_kubectl.cluster_of(health_kubectl.node_names(PROD_NODES)) == "prod"
    assert health_kubectl.cluster_of(health_kubectl.node_names(STAGE_NODES)) == "stage"


def test_cluster_of_is_unknown_for_a_name_from_neither_cluster():
    assert health_kubectl.cluster_of(["some-other-node"]) is None
    assert health_kubectl.cluster_of([]) is None


def test_a_new_node_in_a_cluster_does_not_make_it_unknown():
    """Membership, not equality — otherwise adding a node refuses every gate at once."""
    names = ["daniel-box", "daniel-server", "daniel-3"]
    assert health_kubectl.cluster_of(names) == "prod"


def test_asking_for_the_cluster_this_kubectl_serves_is_clean():
    assert health_kubectl.cluster_refusal("prod", "prod") is None


def test_asking_for_staging_against_a_prod_kubectl_is_flagged():
    """The failure #1663 is about: this returned a healthy prod verdict instead."""
    refusal = health_kubectl.cluster_refusal("stage", "prod")
    assert refusal and "serves the prod cluster, not stage" in refusal


def test_an_unreadable_node_list_is_flagged():
    """Fails closed: a kubectl that cannot say who its nodes are supports no claim at all."""
    assert "cannot confirm" in health_kubectl.cluster_refusal("prod", None)


def test_run_health_refuses_before_it_gates_anything(capsys):
    """The refusal comes first, so no kubectl runs and no workload is read.

    `served=` hands the gate an already-known cluster identity, which is what lets this assert
    the ordering without a cluster and without patching: a run that reached the gate would
    have to talk to kubectl, and the test host has none.
    """
    assert health.run_health("authelia", cluster="stage", served="prod") == 1
    assert "not stage" in capsys.readouterr().out


def test_run_health_still_gates_when_the_cluster_matches(capsys):
    """The accepting half. media-volume declares no rollout-checkable workload, so the gate
    reaches its own verdict by rendering manifests rather than by asking a cluster.

    That dependency is real: if a Deployment ever lands in the media-volume role, this test
    starts reaching for a cluster CI does not have. Pick another workload-free role then.
    """
    assert health.run_health("media-volume", cluster="prod", served="prod") == 1
    assert "no rollout-checkable workload" in capsys.readouterr().out


def test_a_refusal_is_routed_as_a_failure_not_a_skip():
    """The refusal crosses a subprocess boundary and is classified by substring.

    `deploy_detach_notify.py` reads the gate's first stdout line and turns a
    NOT_APPLICABLE_MARKERS match into a `skipped` verdict. A refusal means the gate did not
    run, which must fail the verdict rather than skip it — the same rule the absent-workload
    messages follow, and the only thing connecting these two modules is this assertion.
    """
    from deploy_tools import deploy_detach_notify as notify_mod

    for requested, served in (("stage", "prod"), ("prod", None)):
        message = health_kubectl.cluster_refusal(requested, served)
        assert not [
            marker for marker in notify_mod.NOT_APPLICABLE_MARKERS if marker in message
        ], f"a refusal would be reported as `skipped`: {message}"


def test_every_named_node_is_a_host_in_the_inventory():
    """Non-vacuity against ground truth: a renamed host must fail here, not silently.

    `CLUSTER_NODES` is a constant because probe.py cannot parse Ansible at runtime. Nothing
    but this test notices it drifting from the inventory the names came from.
    """
    hosts = (REPO / "ansible" / "inventory" / "hosts.ini").read_text()
    declared = {
        line.split()[0]
        for line in hosts.splitlines()
        if re.match(r"^[a-z0-9][a-z0-9-]*\s", line)
    }
    named = set().union(*health_kubectl.CLUSTER_NODES.values())
    assert named, (
        "CLUSTER_NODES is empty — every cluster check below would pass vacuously"
    )
    assert named <= declared, (
        f"CLUSTER_NODES names {sorted(named - declared)}, which is in no inventory host line; "
        "the cluster check would call the real cluster unknown and refuse every gate"
    )
