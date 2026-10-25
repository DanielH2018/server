"""Reconcile's activation step: what runs when a packaged file's hash changes (H7.2a).

Each test is one of H7's *Red proofs for the checks this slice adds*, activation entries. The
fake host is `_reconcile_host.py`; `_reconcile_arms.py` ships files and builds the red fixtures.
The clause "homelab-reboot then refuses" belongs to H7.7, which adds that timer: the flag file
this suite asserts is the interface the timer reads.

Run: uv run pytest hosts/lib/tests/test_reconcile_activation.py
"""

import os

import pytest
from _reconcile_arms import PROM, base, live_unit, metrics, red, ship, verbs
from _reconcile_host import Node

SYSCTL_FILE = "/etc/sysctl.d/50-homelab.conf"
SSHD_FILE = "/etc/ssh/sshd_config.d/50-homelab.conf"
FLAG = "run/homelab/validate-failed"


@pytest.fixture
def node(tmp_path) -> Node:
    node = Node(tmp_path)
    return base(node)


def settled(node: Node) -> None:
    result = node.run()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("action", "calls"),
    [
        pytest.param("none", [], id="none"),
        pytest.param("sysctl", [["sysctl", "--system"]], id="sysctl"),
        pytest.param(
            "try-restart:fail2ban.service",
            [
                ["fail2ban-client", "-t"],
                ["systemctl", "try-restart", "fail2ban.service"],
            ],
            id="try-restart",
        ),
        pytest.param(
            "reload:ssh.service",
            [["sshd", "-t"], ["systemctl", "reload", "ssh.service"]],
            id="reload",
        ),
        pytest.param(
            "modules-load",
            [["systemctl", "restart", "systemd-modules-load.service"]],
            id="modules-load",
        ),
    ],
)
def test_a_changed_file_runs_its_action_and_an_unchanged_one_runs_nothing(
    node, action, calls
):
    live_unit(node, "ssh.service")
    live_unit(node, "fail2ban.service")
    live_unit(node, "systemd-modules-load.service")
    path = SYSCTL_FILE if action == "sysctl" else SSHD_FILE
    ship(node, path, "kernel.sysrq = 0\n", action)

    settled(node)

    assert verbs(node) == calls
    assert set(node.applied()["files"]) == {path}

    settled(node)
    assert verbs(node) == [], "an unchanged file must run nothing"

    ship(node, path, "kernel.sysrq = 1\n", action)
    settled(node)
    assert verbs(node) == calls, "a changed file runs its action again"


def test_a_reboot_action_marks_the_host_reboot_pending_and_the_metric_says_so(node):
    ship(node, "/etc/modprobe.d/50-homelab.conf", "options x y=1\n", "reboot")

    settled(node)

    assert (node.root / "run/homelab/reboot-pending").exists()
    assert verbs(node) == [], "a reboot-class change changes nothing live"
    assert 'homelab_host_reboot_pending{host="daniel-pi"} 1' in metrics(node)


def test_a_later_reboot_class_change_keeps_the_time_the_reboot_was_first_owed(node):
    """HostRebootOverdue ages the owed reboot from this series, so a second change must not
    reset it (#6166)."""
    ship(node, "/etc/modprobe.d/50-homelab.conf", "options x y=1\n", "reboot")
    settled(node)
    flag = node.root / "run/homelab/reboot-pending"
    os.utime(flag, (1_000_000, 1_000_000))

    ship(node, "/etc/modprobe.d/50-homelab.conf", "options x y=2\n", "reboot")
    settled(node)

    assert (
        'homelab_host_reboot_pending_since_seconds{host="daniel-pi"} 1000000'
        in metrics(node)
    )


def test_a_host_with_nothing_pending_reads_reboot_pending_zero_not_absent(node):
    settled(node)

    assert 'homelab_host_reboot_pending{host="daniel-pi"} 0' in metrics(node)
    assert "activation_pending" not in metrics(node)


def test_a_manual_action_marks_the_unit_pending_until_it_restarts(node):
    live_unit(node, "docker.service")
    ship(node, "/etc/docker/daemon.json", "{}\n", "manual:docker.service")

    settled(node)

    assert verbs(node) == []
    assert node.applied()["pending"] == {"docker.service": "inv0"}
    assert (
        'homelab_host_activation_pending{host="daniel-pi",unit="docker.service"} 1'
        in metrics(node)
    )

    node.state["units"]["docker.service"]["invocation"] = (
        "inv1"  # the operator restarts it
    )
    settled(node)

    assert node.applied()["pending"] == {}
    assert "activation_pending" not in metrics(node)


def test_an_action_on_a_unit_the_manifest_marks_manual_changes_nothing_live(node):
    live_unit(node, "ssh.service")
    node.manifest["units"]["ssh.service"] = {"policy": "manual"}
    ship(node, SSHD_FILE, "MaxAuthTries 3\n", "reload:ssh.service")

    settled(node)

    assert [v for v in verbs(node) if v[0] == "systemctl"] == []
    assert "ssh.service" in node.applied()["pending"]


def test_a_unit_reconcile_restarted_this_run_gets_no_second_call(node):
    live_unit(node, "fail2ban.service")
    node.manifest["units"]["fail2ban.service"] = {}
    node.write_unit("fail2ban.service", "[Service]\nExecStart=/bin/true\n")
    settled(node)
    node.write_unit("fail2ban.service", "[Service]\nExecStart=/bin/false\n")
    ship(
        node,
        "/etc/fail2ban/jail.d/50-homelab.conf",
        "[sshd]\n",
        "try-restart:fail2ban.service",
    )

    settled(node)

    restarts = [v for v in verbs(node) if v[:2] == ["systemctl", "restart"]]
    assert restarts == [["systemctl", "restart", "fail2ban.service"]]
    assert ["systemctl", "try-restart", "fail2ban.service"] not in verbs(node)


# Validation failures.


def sshd_refuses(node: Node) -> None:
    live_unit(node, "ssh.service")
    node.state["commands"]["sshd"] = "line 3: Bad configuration option: MaxAuthTrys"
    ship(node, SSHD_FILE, "MaxAuthTrys 3\n", "reload:ssh.service")

    result = node.run()

    assert result.returncode == 1, result.stderr
    assert ["systemctl", "reload", "ssh.service"] not in verbs(node)
    assert (node.root / FLAG).exists()
    applied = node.applied()
    assert applied["result"]["ok"] is False
    assert SSHD_FILE not in applied["files"], "a failed entry must not advance"


def test_a_failed_sshd_check_leaves_sshd_untouched_flags_the_host_and_fails_the_run(
    node,
):
    # `result.ok` false is what homelab-updater turns into the hold
    # (test_homelab_updater.py::test_a_run_that_did_not_reconcile_holds_the_host).
    sshd_refuses(node)


def test_red_a_reconcile_that_skips_the_validation_is_caught(tmp_path):
    red(
        tmp_path,
        sshd_refuses,
        "reconcile_lib/activate.py",
        "    command = COMMANDS.get(unit) if kind in",
        "    command = None if kind in",
    )


def test_the_next_run_retries_a_failed_entry_and_clears_the_flag_once_it_passes(node):
    sshd_refuses(node)
    del node.state["commands"]["sshd"]

    settled(node)

    assert not (node.root / FLAG).exists()
    assert ["systemctl", "reload", "ssh.service"] in verbs(node)
    assert SSHD_FILE in node.applied()["files"]


def test_a_revert_that_drops_the_failing_entry_clears_the_flag(node):
    sshd_refuses(node)
    node.manifest["activations"] = {}

    settled(node)

    assert not (node.root / FLAG).exists()


@pytest.mark.parametrize(
    ("action", "path", "text"),
    [
        pytest.param("sysctl", SYSCTL_FILE, "kernel.sysrq 0\n", id="sysctl"),
        pytest.param(
            "try-restart:systemd-journald.service",
            "/etc/systemd/journald.conf.d/50-homelab.conf",
            "SystemMaxUse 1G\n",
            id="journald",
        ),
    ],
)
def test_a_file_reconcile_parses_itself_is_refused_when_a_line_does_not_parse(
    node, action, path, text
):
    live_unit(node, "systemd-journald.service")
    ship(node, path, text, action)

    result = node.run()

    assert result.returncode == 1
    assert verbs(node) == []
    assert (node.root / FLAG).exists()


def test_a_shipped_file_that_is_missing_fails_the_run(node):
    ship(node, SSHD_FILE, "x\n", "none")
    (node.root / SSHD_FILE.lstrip("/")).unlink()

    assert node.run().returncode == 1


def test_a_manifest_with_an_action_reconcile_does_not_know_acts_on_nothing(node):
    node.manifest["activations"] = {SSHD_FILE: "restart-everything"}

    assert node.run().returncode == 1
    assert node.calls("systemctl") == []


def test_the_metrics_file_is_not_written_when_the_exporters_directory_is_missing(
    tmp_path,
):
    node = Node(tmp_path)  # no textfile directory: the updater is what reports that
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-reconcile.service", policy="manual")

    settled(node)

    assert not (node.root / PROM).exists()
    assert not (node.root / PROM).parent.exists()


# Legacy service retirement (H7.2a adds this rule to the legacy-entry step).

LEGACY = "pi-old-check.service"
DRIFT = 'homelab_host_reconcile_drift{host="daniel-pi",arm="legacy"}'


def legacy_service(node: Node, active: str) -> None:
    path = node.root / "etc/systemd/system" / LEGACY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Service]\n")
    node.state["units"][LEGACY] = {
        "load": "loaded",
        "active": active,
        "enabled": "enabled",
    }
    node.manifest["legacy"].append({"unit": LEGACY})


def running_legacy_service_stays(node: Node) -> None:
    legacy_service(node, "active")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [], "a running service is never stopped to retire it"
    assert node.applied()["retired"] == []
    assert f"{DRIFT} 1" in metrics(node)


def test_a_legacy_service_that_is_running_stays_unretired_as_drift_with_no_hold(node):
    running_legacy_service_stays(node)


def test_red_a_legacy_step_that_retires_the_running_service_is_caught(tmp_path):
    red(
        tmp_path,
        running_legacy_service_stays,
        "homelab-reconcile",
        "if key not in prev_retired and not waiting and running_service(entry):",
        "if False:",
    )


def test_a_legacy_service_that_is_still_starting_counts_as_running(node):
    legacy_service(node, "activating")

    assert node.run().returncode == 0
    assert node.actions() == []


def test_the_same_service_once_inactive_is_disabled_stopped_removed_and_retired(node):
    legacy_service(node, "active")
    node.run()
    node.state["units"][LEGACY]["active"] = "inactive"

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["disable", LEGACY], ["stop", LEGACY]]
    assert len(node.applied()["retired"]) == 1
    assert not (node.root / "etc/systemd/system" / LEGACY).exists()
    assert f"{DRIFT} 0" in metrics(node)
