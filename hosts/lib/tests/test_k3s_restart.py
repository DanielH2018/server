"""`homelab-k3s-restart`: one gated restart per changed config hash, and what it records.

The script runs in-process against a fake root, a stub `systemctl` and a stub API
(`_k3s_restart_host.py`). Each refusal has a passing twin that differs in one place.
"""

import ast
import json

import pytest
from _k3s_gates_fixtures import job, kustomization, lease, listing, plan
from _k3s_restart_host import SCRIPT, Box

import k3s_gates


@pytest.fixture
def box(tmp_path, monkeypatch) -> Box:
    return Box(tmp_path, monkeypatch)


def owed(box: Box) -> Box:
    """A box that applied one hash and has since changed its config, so a restart is owed."""
    box.set_state("applied.sha256", "0" * 64)
    return box


# ── no restart without a changed hash ───────────────────────────────────────────────────────


def test_the_first_run_records_the_hash_and_restarts_nothing(box):
    assert box.run() == 0
    assert box.state("applied.sha256") == box.hash()
    assert box.restarts() == 0
    assert box.metrics() == {
        "pending": 0,
        "failed": 0,
        "last_success_timestamp_seconds": box.now,
    }


def test_an_empty_hash_set_is_a_valid_hash_and_not_a_failure(box):
    (box.root / box.mod.MANIFEST).write_text(json.dumps({"activations": {}}))
    assert box.run() == 0
    assert box.state("applied.sha256")
    assert box.run() == 0
    assert box.metrics()["pending"] == 0


def test_an_unchanged_hash_restarts_nothing(box):
    box.run()
    assert box.run() == 0
    assert box.restarts() == 0
    assert box.metrics()["pending"] == 0


def test_an_unreadable_manifest_or_file_stops_the_run_and_reports_pending(box):
    box.run()
    (box.root / box.mod.MANIFEST).write_text("not json")
    assert box.run() == 1
    assert box.metrics()["pending"] == 1
    assert box.restarts() == 0


def test_a_shipped_file_that_is_missing_stops_the_run(box):
    box.run()
    (box.root / "etc/rancher/k3s/config.yaml.d/50-homelab.yaml").unlink()
    assert box.run() == 1
    assert box.restarts() == 0


# ── the gates ───────────────────────────────────────────────────────────────────────────────


def changed(box: Box) -> Box:
    owed(box)
    box.write_config("a: 2\n")
    return box


def test_a_changed_hash_with_passing_gates_restarts_once_and_records_it(box):
    changed(box)
    assert box.run("--deploy-lock") == 0
    assert box.restarts() == 1
    assert box.state("applied.sha256") == box.hash()
    assert box.state("attempt.sha256") is None
    assert box.metrics()["pending"] == 0
    assert box.metrics()["failed"] == 0


def test_a_changed_hash_with_a_refusing_gate_writes_pending_and_restarts_nothing(box):
    changed(box)
    box.routes[k3s_gates.JOBS_PATH] = listing(
        job("apply-k3s-server-abc", condition=None)
    )
    assert box.run("--deploy-lock") == 0
    assert box.restarts() == 0
    assert box.metrics()["pending"] == 1
    assert box.state("applied.sha256") == "0" * 64


def test_a_plan_that_is_applying_refuses(box):
    changed(box)
    box.routes[f"{k3s_gates.PLANS_PATH}/k3s-server"] = plan(
        "k3s-server", applying=["daniel-box"]
    )
    box.run("--deploy-lock")
    assert box.restarts() == 0 and box.metrics()["pending"] == 1


def test_a_missing_plan_refuses(box):
    changed(box)
    del box.routes[f"{k3s_gates.PLANS_PATH}/k3s-agent"]
    box.run("--deploy-lock")
    assert box.restarts() == 0 and box.metrics()["pending"] == 1


def test_a_promotion_in_flight_refuses(box):
    changed(box)
    lagging = kustomization(
        "homelab",
        status={
            "observedGeneration": 3,
            "lastAppliedRevision": "prod@sha256:old",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    )
    box.routes[k3s_gates.KUSTOMIZATIONS_PATH] = listing(lagging)
    box.run("--deploy-lock")
    assert box.restarts() == 0 and box.metrics()["pending"] == 1


def test_an_agent_host_run_refuses_on_a_recent_server_restart(box):
    changed(box)
    box.routes[k3s_gates.LEASE_PATH] = lease(acquired_s=300)
    box.run("--guard-server-restart")
    assert box.restarts() == 0 and box.metrics()["pending"] == 1


def test_an_agent_host_run_passes_a_lease_acquired_twenty_minutes_ago(box):
    changed(box)
    box.routes[k3s_gates.LEASE_PATH] = lease(acquired_s=1200, renewed_s=5)
    assert box.run("--guard-server-restart") == 0
    assert box.restarts() == 1


def test_the_guard_runs_only_when_asked(box):
    changed(box)
    box.routes[k3s_gates.LEASE_PATH] = lease(acquired_s=300)
    assert box.run() == 0
    assert box.restarts() == 1


# ── the deployer's lock and hold, until H7.19a ──────────────────────────────────────────────


def test_a_held_deploy_lock_refuses(box):
    import fcntl
    import os

    changed(box)
    fd = os.open(box.root / "run/lock/server-deploy-all.lock", os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        box.run("--deploy-lock")
    finally:
        os.close(fd)
    assert box.restarts() == 0 and box.metrics()["pending"] == 1


def test_an_absent_lock_file_refuses_and_a_deployer_hold_refuses(box):
    changed(box)
    (box.root / "run/lock/server-deploy-all.lock").unlink()
    box.run("--deploy-lock")
    assert box.restarts() == 0
    (box.root / "run/lock/server-deploy-all.lock").touch()
    (box.root / "var/lib/gitops-deploy/hold_sha").write_text("abc123\n")
    box.run("--deploy-lock")
    assert box.restarts() == 0


def test_a_run_without_the_lock_flag_ignores_the_deployer(box):
    changed(box)
    (box.root / "run/lock/server-deploy-all.lock").unlink()
    assert box.run() == 0
    assert box.restarts() == 1


# ── the restart itself ──────────────────────────────────────────────────────────────────────


def test_a_stale_ready_heartbeat_is_not_a_success(box):
    changed(box)
    box.beats_after_restart = False
    assert box.run() == 1
    assert box.state("failed.sha256") == box.hash()
    assert box.state("applied.sha256") == "0" * 64
    assert box.metrics()["failed"] == 1


def test_an_active_unit_with_an_unchanged_main_pid_is_not_a_success(box):
    changed(box)
    box.pid_after_restart = box.pid
    assert box.run() == 1
    assert box.state("failed.sha256") == box.hash()


def test_a_unit_that_never_reads_active_is_not_a_success(box):
    changed(box)
    box.active = "activating"
    assert box.run() == 1


def test_a_failed_hash_is_not_retried(box):
    changed(box)
    box.beats_after_restart = False
    box.run()
    box.calls.clear()
    assert box.run() == 0
    assert box.restarts() == 0
    assert box.metrics()["failed"] == 1


def test_a_revert_after_a_failure_keeps_failed_until_the_operator_removes_it(box):
    box.run()
    good = box.hash()
    box.write_config("a: 2\n")
    box.beats_after_restart = False
    box.run()
    box.write_config("a: 1\n")
    assert box.hash() == good
    assert box.run() == 0
    assert box.restarts() == 1
    assert box.metrics()["failed"] == 1
    (box.root / box.mod.STATE_DIR / "failed.sha256").unlink()
    box.run()
    assert box.metrics()["failed"] == 0


def test_a_killed_attempt_is_a_failure_on_the_next_run(box):
    changed(box)
    box.set_state("attempt.sha256", box.hash())
    assert box.run() == 0
    assert box.restarts() == 0
    assert box.state("failed.sha256") == box.hash()
    assert box.state("attempt.sha256") is None
    assert box.metrics()["failed"] == 1


def test_exec_stop_post_after_a_timeout_records_the_attempt_at_once(box, monkeypatch):
    box.set_state("attempt.sha256", "f" * 64)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    assert box.mod.main(["--stop-post", "--host", "daniel-box"]) == 0
    assert box.state("failed.sha256") == "f" * 64
    assert box.state("attempt.sha256") is None
    assert box.metrics()["failed"] == 1


def test_exec_stop_post_after_success_changes_nothing(box, monkeypatch):
    box.set_state("attempt.sha256", "f" * 64)
    monkeypatch.setenv("SERVICE_RESULT", "success")
    box.mod.main(["--stop-post", "--host", "daniel-box"])
    assert box.state("attempt.sha256") == "f" * 64


def test_the_metrics_temp_file_does_not_end_in_prom(box):
    assert box.mod.TMP.endswith(".tmp") and not box.mod.TMP.endswith(".prom")
    box.run()
    names = {p.name for p in (box.root / "var/lib/node-exporter-textfile").iterdir()}
    assert names == {"homelab_k3s_restart.prom"}


def test_a_refusing_run_writes_all_three_series(box):
    changed(box)
    box.routes[k3s_gates.JOBS_PATH] = listing()
    box.run("--deploy-lock")
    assert set(box.metrics()) == {"pending", "failed", "last_success_timestamp_seconds"}


# ── the script's own rules ──────────────────────────────────────────────────────────────────


def lock_opener_violations(source: str) -> list[str]:
    """Any use of a creating or writing open flag, and any `os.open` without O_RDONLY."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {
            "O_CREAT",
            "O_WRONLY",
            "O_RDWR",
        }:
            found.append(node.attr)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.open":
            if "O_RDONLY" not in ast.unparse(node):
                found.append("os.open without O_RDONLY")
    return found


def test_the_restart_script_never_creates_or_writes_the_lock():
    source = SCRIPT.read_text()
    assert "os.open(" in source, "the opener this test polices is gone"
    assert lock_opener_violations(source) == []


@pytest.mark.parametrize(
    "mutation",
    ["os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)", "os.open(lock, os.O_WRONLY)"],
)
def test_a_restart_script_that_creates_the_lock_is_flagged(mutation):
    source = SCRIPT.read_text().replace(
        "fd = os.open(lock, os.O_RDONLY)", f"fd = {mutation}", 1
    )
    assert mutation in source
    assert lock_opener_violations(source)


def test_the_script_parses_with_the_python_312_grammar():
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


def test_the_script_names_the_marker_prepare_reads():
    # `prepare` refuses while this directory exists; the unit's RuntimeDirectory= creates it.
    assert k3s_gates.RESTART_MARKER == "/run/homelab-k3s-restart"
    assert (
        "RuntimeDirectory=homelab-k3s-restart"
        in (
            SCRIPT.parents[2]
            / "hosts/components/k3s-restart/units/homelab-k3s-restart.service.j2"
        ).read_text()
    )
