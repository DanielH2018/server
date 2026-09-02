"""Tests for the live half of gen_infra_map: what the cluster and the Pi report.

The parsers turn `docker ps`, `kubectl get` and Longhorn backup-target output into the
records the model reconciles, and the collectors decide what to run and what to do when
a tool is missing. Every parser has a bad-input case, because a collector that returns a
clean empty result on garbage renders as "everything is missing", which is the false alarm
the page is guarding against.

Run: uv run pytest scripts/infra_map/tests/test_infra_map_live.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import gen_infra_map as g
import infra_map_live as live

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


def test_collect_k8s_asks_for_every_long_running_kind(monkeypatch):
    """The inventory excuses a role that declares none of these kinds, so the
    collector must fetch all of them or a declared kind becomes a false Missing."""
    seen = []

    def fake_run(argv, timeout):
        seen.append(argv)
        return True, json.dumps({"items": []})

    monkeypatch.setattr(live, "find_tool", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(live, "find_kubeconfig", lambda: Path("/tmp/kubeconfig"))
    monkeypatch.setattr(live, "_run", fake_run)
    ok, workloads, err = live.collect_k8s("box", "box")
    assert ok and workloads == {} and err == ""
    requested = set(seen[0][seen[0].index("get") + 1].split(","))
    assert requested == {k.lower() + "s" for k in g.LONG_RUNNING_KINDS}


def test_find_tool_looks_beyond_an_impoverished_path(monkeypatch):
    """The cron-PATH case: /usr/local/bin must be searched even when PATH omits it."""
    kubectl = Path("/usr/local/bin/kubectl")
    if not kubectl.exists():
        pytest.skip("kubectl not installed at the path this guards")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert g.find_tool("kubectl") == str(kubectl)


def test_find_tool_returns_none_for_a_genuinely_absent_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert g.find_tool("definitely-not-a-real-binary") is None


def test_collect_k8s_raises_rather_than_reporting_a_clean_empty_result(monkeypatch):
    """A missing binary is a broken setup, not 'the cluster has no deployments'."""
    monkeypatch.setattr(live, "find_tool", lambda name: None)
    with pytest.raises(g.MissingToolError):
        g.collect_k8s("box", "box")


def test_collect_docker_raises_when_ssh_is_absent(monkeypatch):
    monkeypatch.setattr(live, "find_tool", lambda name: None)
    with pytest.raises(g.MissingToolError):
        g.collect_docker("daniel-server", "daniel-box")


def test_find_kubeconfig_prefers_an_explicit_kubeconfig_env(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("cfg")
    monkeypatch.setenv("KUBECONFIG", str(explicit))
    assert g.find_kubeconfig() == explicit


def test_find_kubeconfig_reads_kubeconfig_as_a_path_list(monkeypatch, tmp_path):
    """KUBECONFIG is a colon-separated list; a bare Path() of it opens nothing."""
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    second.write_text("cfg")
    monkeypatch.setenv("KUBECONFIG", f"{first}:{second}")
    assert g.find_kubeconfig() == second


def test_find_kubeconfig_skips_an_unreadable_candidate(monkeypatch, tmp_path):
    """The actual cron failure: the k3s default exists but is root-only 0640."""
    unreadable = tmp_path / "root-only.yaml"
    unreadable.write_text("cfg")
    unreadable.chmod(0o000)
    readable = tmp_path / "mine.yaml"
    readable.write_text("cfg")
    monkeypatch.setenv("KUBECONFIG", f"{unreadable}:{readable}")
    assert g.find_kubeconfig() == readable


def test_collect_k8s_raises_when_no_kubeconfig_is_readable(monkeypatch):
    """Must not degrade to 'declared only' — that renders as a healthy page."""
    monkeypatch.setattr(live, "find_tool", lambda name: "/usr/local/bin/kubectl")
    monkeypatch.setattr(live, "find_kubeconfig", lambda: None)
    with pytest.raises(g.MissingToolError):
        g.collect_k8s("box", "box")


def test_collect_k8s_passes_the_resolved_kubeconfig_to_kubectl(monkeypatch, tmp_path):
    """Explicit --kubeconfig is the point: kubectl's own lookup varies by caller."""
    cfg = tmp_path / "kube.yaml"
    cfg.write_text("cfg")
    seen = {}

    def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        return True, json.dumps({"items": []})

    monkeypatch.setattr(live, "find_tool", lambda name: "/usr/local/bin/kubectl")
    monkeypatch.setattr(live, "find_kubeconfig", lambda: cfg)
    monkeypatch.setattr(live, "_run", fake_run)
    g.collect_k8s("box", "box")
    assert "--kubeconfig" in seen["cmd"]
    assert str(cfg) in seen["cmd"]


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
        loaded += yaml.safe_load(path.read_text()) or []
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
