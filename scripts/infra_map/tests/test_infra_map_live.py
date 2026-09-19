"""Tests for the live half of gen_infra_map: what the cluster and the Pi report.

The parsers turn `docker ps`, `kubectl get` and Longhorn backup-target output into the
records the model reconciles, and the collectors decide what to run and what to do when
a tool is missing. Every parser has a bad-input case, because a collector that returns a
clean empty result on garbage renders as "everything is missing", which is the false alarm
the page is guarding against.

Run: uv run pytest scripts/infra_map/tests/test_infra_map_live.py
"""

import json
import subprocess

import pytest
from lib import kubectl as kubectl_lib
from lib import yaml_fast

import gen_infra_map as g
from infra_map import live

from _infra_map import (
    REPO_ROOT,
    backup_target,
    daemonset,
    deployment,
    node,
)


def test_parse_docker_ps_reads_health_from_the_status_string():
    out = "a\trunning\tUp 2 days (healthy)\timg:1\nb\trunning\tUp 1 day (unhealthy)\timg:2\n"
    parsed = g.parse_docker_ps(out)
    assert parsed["a"]["healthy"] is True
    assert parsed["b"]["unhealthy"] is True


def test_parse_docker_ps_ignores_malformed_and_blank_lines():
    assert g.parse_docker_ps("garbage\n\n\tx\t\t\n") == {}


def test_parse_kubectl_workloads_extracts_replica_counts():
    payload = json.dumps({"items": [deployment("traefik", ready=1, desired=2)]})
    parsed = g.parse_kubectl_workloads(payload)
    assert parsed[("homelab", "traefik")]["ready"] == 1
    assert parsed[("homelab", "traefik")]["desired"] == 2


def test_parse_kubectl_workloads_treats_absent_ready_replicas_as_zero():
    """kubectl omits readyReplicas entirely at zero — None would break the sum."""
    payload = json.dumps({"items": [deployment("down", ready=0)]})
    assert g.parse_kubectl_workloads(payload)[("homelab", "down")]["ready"] == 0


def test_parse_kubectl_workloads_returns_empty_on_bad_json():
    assert g.parse_kubectl_workloads("not json") == {}


def test_parse_kubectl_workloads_reads_a_daemonset_from_its_status_counts():
    """node-exporter and dri-device-plugin read as missing while the map fetched
    Deployments alone; a DaemonSet's desired count is what the scheduler placed."""
    payload = json.dumps({"items": [daemonset("node-exporter", ready=1, desired=2)]})
    parsed = g.parse_kubectl_workloads(payload)
    assert parsed[("homelab", "node-exporter")] == {
        "kind": "DaemonSet",
        "ready": 1,
        "desired": 2,
        "image": "img:1",
    }


def test_parse_kubectl_workloads_reads_a_statefulset_like_a_deployment():
    item = {**deployment("db", ready=1, desired=1), "kind": "StatefulSet"}
    parsed = g.parse_kubectl_workloads(json.dumps({"items": [item]}))
    assert parsed[("homelab", "db")]["kind"] == "StatefulSet"
    assert parsed[("homelab", "db")]["ready"] == 1


PROD_NODES = json.dumps(
    {"items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]}
)


class _FakeCluster:
    """Discovery resolves and `get nodes` answers as prod; every other call returns `{}`.

    `tools` is the `lib.kubectl.Tools` to hand the collector, `seen` the argvs it ran. The
    collector goes through the shared invoker since #2062, so its seams are the invoker's,
    not a private `_run`.
    """

    def __init__(self, kubeconfig, binary="/usr/local/bin/kubectl"):
        self.seen = []
        self.tools = kubectl_lib.Tools(
            run=self._run,
            find_tool=lambda name: binary,
            find_kubeconfig=lambda: kubeconfig,
        )

    def _run(self, argv, timeout):
        self.seen.append(argv)
        stdout = PROD_NODES if argv[-4:] == ["get", "nodes", "-o", "json"] else "{}"
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture
def fake_cluster(tmp_path):
    cfg = tmp_path / "kube.yaml"
    cfg.write_text("cfg")
    kubectl_lib.forget_served_cluster()
    yield _FakeCluster(cfg)
    kubectl_lib.forget_served_cluster()


def test_collect_k8s_asks_for_every_long_running_kind(fake_cluster):
    """The inventory excuses a role that declares none of these kinds, so the
    collector must fetch all of them or a declared kind becomes a false Missing."""
    ok, workloads, err = live.collect_k8s(
        "daniel-box", "daniel-box", fake_cluster.tools
    )
    assert ok and workloads == {} and err == ""
    argv = fake_cluster.seen[-1]
    requested = set(argv[argv.index("get") + 1].split(","))
    assert requested == {k.lower() + "s" for k in g.LONG_RUNNING_KINDS}


def test_collect_k8s_names_the_cluster_its_host_stands_in(fake_cluster):
    """A host in no known cluster gets no kubectl at all — there is no cluster to name."""
    ok, workloads, err = live.collect_k8s("elsewhere", "elsewhere", fake_cluster.tools)
    assert (ok, workloads) == (False, {}) and "no known cluster" in err
    assert fake_cluster.seen == []


def test_collect_k8s_reports_a_wrong_cluster_as_an_observation(fake_cluster):
    """The refusal renders on the page as the error rather than blinding the map.

    daniel-stage names the stage cluster, and the fake's nodes are production's.
    """
    ok, workloads, err = live.collect_k8s(
        "daniel-stage", "daniel-stage", fake_cluster.tools
    )
    assert (ok, workloads) == (False, {}) and "not stage" in err


def test_collect_k8s_raises_rather_than_reporting_a_clean_empty_result(fake_cluster):
    """A missing binary is a broken setup, not 'the cluster has no deployments'."""
    tools = kubectl_lib.Tools(find_tool=lambda name: None)
    with pytest.raises(g.MissingToolError):
        g.collect_k8s("daniel-box", "daniel-box", tools)


def test_collect_docker_raises_when_ssh_is_absent(monkeypatch):
    monkeypatch.setattr(live, "find_tool", lambda name: None)
    with pytest.raises(g.MissingToolError):
        g.collect_docker("daniel-server", "daniel-box")


def test_collect_k8s_raises_when_no_kubeconfig_is_readable(fake_cluster):
    """Must not degrade to 'declared only' — that renders as a healthy page."""
    tools = kubectl_lib.Tools(
        find_tool=lambda name: "/usr/local/bin/kubectl", find_kubeconfig=lambda: None
    )
    with pytest.raises(g.MissingToolError):
        g.collect_k8s("daniel-box", "daniel-box", tools)


def test_collect_k8s_passes_the_resolved_kubeconfig_to_kubectl(fake_cluster):
    """Explicit --kubeconfig is the point: kubectl's own lookup varies by caller."""
    g.collect_k8s("daniel-box", "daniel-box", fake_cluster.tools)
    argv = fake_cluster.seen[-1]
    assert "--kubeconfig" in argv
    assert argv[argv.index("--kubeconfig") + 1].endswith("kube.yaml")


def test_refresh_cron_sets_kubeconfig():
    """Second layer, same as PATH: pin it where the regression actually happened."""
    job = _refresh_cron_job()
    assert "KUBECONFIG=" in job, "kubectl would fall back to the root-only k3s config"


def _refresh_cron_job():
    # main.yml became a list of import_tasks in the 2026-08-15 split, so scan every task
    # file in the directory — the cron now lives in crons.yml, not the entry point.
    task_dir = REPO_ROOT / "ansible/roles/setup/initial_setup/tasks"
    if not task_dir.is_dir():
        pytest.skip("ansible role tree not present")
    loaded = []
    for path in sorted(task_dir.glob("*.yml")):
        loaded += yaml_fast.safe_load(path.read_text()) or []
    jobs = [
        t["ansible.builtin.cron"]["job"]
        for t in loaded
        if isinstance(t, dict) and "infra-map" in (t.get("tags") or [])
    ]
    assert jobs, "the infra-map refresh cron has gone missing"
    return jobs[0]


def test_refresh_cron_puts_usr_local_bin_on_the_path():
    """Second layer of the same guard, pinned where the regression happened."""
    job = _refresh_cron_job()
    assert "/usr/local/bin" in job, "kubectl would not resolve under cron's PATH"


def test_parse_kubectl_nodes_reads_readiness_roles_and_address():
    payload = json.dumps(
        {"items": [node("daniel-box", roles=("control-plane", "etcd"))]}
    )
    parsed = g.parse_kubectl_nodes(payload)
    assert parsed["daniel-box"]["ready"] is True
    assert parsed["daniel-box"]["roles"] == ["control-plane", "etcd"]
    assert parsed["daniel-box"]["ip"] == "10.0.0.1"


def test_parse_kubectl_nodes_reads_only_the_exact_node_role_group():
    """A neighbouring label group must not contribute a role.

    The role filter compares the label key's group exactly. A prefix test would behave the
    same here, so this pins which of the two the code performs.
    """
    payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "daniel-box",
                        "labels": {
                            "node-role.kubernetes.io/etcd": "true",
                            "node-role.kubernetes.io.example.com/spoofed": "true",
                            "node-role.kubernetes.io": "true",
                        },
                    },
                    "spec": {},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}],
                        "nodeInfo": {"kubeletVersion": "v1.36.2+k3s1"},
                    },
                }
            ]
        }
    )
    assert g.parse_kubectl_nodes(payload)["daniel-box"]["roles"] == ["etcd"]


def test_parse_kubectl_nodes_marks_a_not_ready_node():
    """A NotReady node reading as healthy is the miss this collection exists to catch."""
    payload = json.dumps({"items": [node("daniel-server", ready=False)]})
    assert g.parse_kubectl_nodes(payload)["daniel-server"]["ready"] is False


def test_parse_kubectl_nodes_returns_empty_on_bad_json():
    assert g.parse_kubectl_nodes("not json") == {}


def test_parse_pod_placement_blanks_an_unscheduled_pod():
    """kubectl prints <none> for a pod with no node; that is not a node name."""
    out = "homelab traefik-abc daniel-box Running\nhomelab pending-x <none> Pending\n"
    parsed = g.parse_pod_placement(out)
    assert parsed[0]["node"] == "daniel-box"
    assert parsed[1]["node"] == ""


def test_parse_pod_placement_ignores_short_lines():
    assert g.parse_pod_placement("garbage\n\n") == []


def test_parse_backup_targets_separates_disarmed_from_unavailable():
    """A blank URL is how this repo disarms a target, not how one breaks."""
    payload = json.dumps(
        {
            "items": [
                backup_target("default", url=""),
                backup_target("r2", available=False),
            ]
        }
    )
    parsed = {t["name"]: t for t in g.parse_backup_targets(payload)}
    assert parsed["default"]["armed"] is False
    assert parsed["r2"]["armed"] is True
    assert parsed["r2"]["available"] is False


def test_parse_backup_targets_returns_empty_on_bad_json():
    assert g.parse_backup_targets("not json") == []
