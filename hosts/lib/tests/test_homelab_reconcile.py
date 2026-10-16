"""`hosts/lib/homelab-reconcile` against a stub `systemctl` and a stub dpkg lock.

Each test is one of H3's *Verification* reconcile cases. A case that needs a previous state
runs reconcile once to write it, changes the package, and runs again. `_reconcile_host.py` is
the fake host.

Run: uv run pytest hosts/lib/tests/test_homelab_reconcile.py
"""

import fcntl
import os
import time

import pytest
from _reconcile_host import V1, Node

V2 = "0.101+gbbbbbbbbbbbb"


@pytest.fixture
def node(tmp_path) -> Node:
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-updater.timer")
    node.unit("homelab-reconcile.service", policy="manual")
    return node


def settled(node: Node) -> None:
    """Run reconcile once, so the next run has a previous state to compare with."""
    result = node.run()
    assert result.returncode == 0, result.stderr


# Step 1: stopping what the package dropped.


def test_a_unit_dropped_from_the_manifest_is_stopped(node):
    node.unit("homelab-pi-gz.service", active="active")
    node.unit("homelab-pi-sd.service", active="active")
    settled(node)
    node.drop("homelab-pi-gz.service")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["stop", "homelab-pi-gz.service"]]
    assert "homelab-pi-gz.service" not in node.applied()["units"]


def test_a_loaded_homelab_unit_whose_file_has_gone_is_stopped(node):
    # No manifest ever named it, so only its LoadState marks it.
    node.state["units"]["homelab-stray.service"] = {
        "load": "not-found",
        "active": "active",
        "enabled": "disabled",
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["stop", "homelab-stray.service"]]


def test_a_timer_whose_link_was_dropped_is_stopped_and_its_service_is_not(node):
    node.unit("homelab-pi-sd.timer", active="active", linked=True)
    node.unit("homelab-pi-sd.service", active="active")
    settled(node)
    node.unlink("homelab-pi-sd.timer")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["stop", "homelab-pi-sd.timer"]]
    assert node.state["units"]["homelab-pi-sd.service"]["active"] == "active"


# Step 3: the restart policy for a changed unit.


def test_a_changed_active_service_is_try_restarted_and_an_enabled_timer_restarted(node):
    node.unit("homelab-pi-sd.service", active="active")
    node.unit("homelab-pi-sd.timer", active="active", linked=True)
    node.unit("homelab-pi-gz.service")
    settled(node)
    for name in (
        "homelab-pi-sd.service",
        "homelab-pi-sd.timer",
        "homelab-pi-gz.service",
    ):
        node.write_unit(name, "[Unit]\nDescription=changed\n")

    result = node.run()

    assert result.returncode == 0, result.stderr
    # The inactive service with no enable link is left for its next start.
    assert node.actions() == [
        ["try-restart", "homelab-pi-sd.service"],
        ["restart", "homelab-pi-sd.timer"],
    ]


def test_an_inactive_timer_with_no_link_stays_inactive_when_its_dropin_changes(node):
    # The Pi's homelab-updater.timer before H3.9b: a drop-in, and no enable link.
    node.write_dropin("homelab-updater.timer", "[Timer]\nOnCalendar=\n")
    first = node.run()
    node.write_dropin(
        "homelab-updater.timer", "[Timer]\nOnCalendar=\nOnCalendar=*:05\n"
    )

    second = node.run()

    assert first.returncode == second.returncode == 0, second.stderr
    assert node.actions() == []
    assert node.state["units"]["homelab-updater.timer"]["active"] == "inactive"


def test_a_first_run_with_no_state_leaves_an_unlinked_timer_inactive(node):
    node.write_dropin("homelab-updater.timer", "[Timer]\nOnCalendar=\n")
    assert not node.applied_path.exists()

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == []
    assert node.state["units"]["homelab-updater.timer"]["active"] == "inactive"


def test_a_disarmed_recovery_timer_stays_stopped_when_its_file_changes_later(node):
    node.unit("homelab-pi-recovery.timer", active="active", linked=True)
    settled(node)
    node.unlink("homelab-pi-recovery.timer")
    settled(node)
    node.write_unit("homelab-pi-recovery.timer", "[Timer]\nOnBootSec=10min\n")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == []
    assert node.state["units"]["homelab-pi-recovery.timer"]["active"] == "inactive"


def test_a_masked_timer_whose_hash_changed_is_never_touched_and_the_run_succeeds(node):
    node.unit("homelab-pi-sd.timer", active="active", linked=True)
    settled(node)
    node.state["units"]["homelab-pi-sd.timer"].update(load="masked", active="inactive")
    node.write_unit("homelab-pi-sd.timer", "[Timer]\nOnCalendar=hourly\n")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == []
    assert node.applied()["result"]["ok"] is True


def test_a_manual_service_is_never_restarted(node):
    node.state["units"]["homelab-updater.service"]["active"] = "active"
    settled(node)
    node.write_unit("homelab-updater.service", "[Service]\nExecStart=/bin/true\n")
    # The manifest's flag is not what keeps it: reconcile names both services itself.
    node.manifest["units"]["homelab-updater.service"] = {}

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == []


def test_a_masked_unit_is_never_started_and_an_unmasked_one_is(node):
    node.unit("homelab-pi-sd.timer", linked=True, load="masked")
    node.unit("homelab-pi-gz.timer", linked=True)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert ["start", "homelab-pi-gz.timer"] in node.actions()
    assert ["start", "homelab-pi-sd.timer"] not in node.actions()


# Step 4: legacy entries.


def test_a_legacy_cron_line_is_removed_and_the_rest_of_the_crontab_kept(node):
    node.state["crontabs"]["ubuntu"] = (
        "#Ansible: pi-sd-health\n*/5 * * * * /usr/local/bin/pi-sd-health.sh\n"
        "#Ansible: other\n0 1 * * * /bin/true\n"
    )
    node.manifest["legacy"].append({"cron": "pi-sd-health", "user": "ubuntu"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.state["crontabs"]["ubuntu"] == "#Ansible: other\n0 1 * * * /bin/true\n"
    assert len(node.applied()["retired"]) == 1


def test_a_legacy_cron_on_a_host_without_crontab_counts_as_retired(node):
    # 127 is what `run` returns when it cannot exec the binary, and what a shell returns for
    # a command it cannot find. The container test image installs no cron package.
    (node.bin / "crontab").write_text("#!/bin/sh\nexit 127\n")
    node.manifest["legacy"].append({"cron": "pi-sd-health", "user": "ubuntu"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert len(node.applied()["retired"]) == 1


def test_a_crontab_that_fails_otherwise_leaves_the_cron_unretired(node):
    (node.bin / "crontab").write_text(
        "#!/bin/sh\necho 'permission denied' >&2\nexit 1\n"
    )
    node.manifest["legacy"].append({"cron": "pi-sd-health", "user": "ubuntu"})

    result = node.run()

    assert result.returncode != 0
    assert node.applied()["retired"] == []


def test_a_path_dpkg_reports_as_owned_is_never_removed_and_an_unowned_one_is(node):
    for name in ("owned", "stray"):
        path = node.root / "usr/local/bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        node.manifest["legacy"].append({"path": f"/usr/local/bin/{name}"})
    node.state["owned"] = ["/usr/local/bin/owned"]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert (node.root / "usr/local/bin/owned").exists()
    assert not (node.root / "usr/local/bin/stray").exists()


def test_an_ansible_era_unit_is_disabled_stopped_and_removed_unless_a_package_owns_it(
    node,
):
    for name in ("pi-sd-health.timer", "owned.timer"):
        path = node.root / "etc/systemd/system" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Timer]\n")
        node.state["units"][name] = {
            "load": "loaded",
            "active": "active",
            "enabled": "enabled",
        }
        node.manifest["legacy"].append({"unit": name})
    node.state["owned"] = ["/etc/systemd/system/owned.timer"]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [
        ["disable", "pi-sd-health.timer"],
        ["stop", "pi-sd-health.timer"],
    ]
    assert not (node.root / "etc/systemd/system/pi-sd-health.timer").exists()
    assert (node.root / "etc/systemd/system/owned.timer").exists()
    assert len(node.applied()["retired"]) == 2


# The dpkg lock and the record.


def _waiting_on(lock: os.PathLike) -> bool:
    """True when some process is blocked in a lock request on `lock`'s inode."""
    inode = os.stat(lock).st_ino
    with open("/proc/locks") as locks:
        return any("->" in line and f":{inode} " in line for line in locks)


def test_started_while_the_lock_is_held_reconcile_waits_and_reads_the_later_manifest(
    node,
):
    node.unit("homelab-pi-sd.timer", linked=False)
    with node.lock.open("r+") as held:
        fcntl.lockf(held, fcntl.LOCK_EX)
        process = node.start()
        deadline = time.monotonic() + 30
        while not _waiting_on(node.lock):
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "reconcile never waited on the lock"
            time.sleep(0.01)
        assert node.calls("systemctl") == []
        # The transaction holding the lock ships a link before it ends.
        node.manifest["enable_links"].append("homelab-pi-sd.timer")
        node.write_manifest()

    assert node.finish(process) == 0
    assert ["start", "homelab-pi-sd.timer"] in node.actions()
    assert all(call["lock_free"] for call in node.calls("systemctl"))


def test_a_package_not_ii_records_a_failed_result_and_acts_on_nothing(node):
    node.unit("homelab-pi-sd.timer", linked=True)
    node.state["installed"]["homelab-daniel-pi"] = ["iU ", V2]

    result = node.run()

    assert result.returncode == 1
    assert node.actions() == []
    record = node.applied()["result"]
    assert record["ok"] is False
    assert record["versions"] == {"homelab-updater": V1, "homelab-daniel-pi": V2}
    assert "last_ok" not in node.applied()


def test_a_failed_restart_keeps_its_old_hash_and_the_next_run_retries_only_it(node):
    node.unit("homelab-pi-sd.service", active="active")
    node.unit("homelab-pi-gz.service", active="active")
    settled(node)
    before = node.applied()
    for name in ("homelab-pi-sd.service", "homelab-pi-gz.service"):
        node.write_unit(name, "[Service]\nExecStart=/bin/false\n")
    node.state["fail"] = ["try-restart homelab-pi-sd.service"]

    failed = node.run()

    assert failed.returncode == 1
    applied = node.applied()
    assert applied["result"]["ok"] is False
    assert applied["last_ok"] == before["result"]
    assert (
        applied["units"]["homelab-pi-sd.service"]
        == before["units"]["homelab-pi-sd.service"]
    )
    assert (
        applied["units"]["homelab-pi-gz.service"]
        != before["units"]["homelab-pi-gz.service"]
    )

    node.state["fail"] = []
    retried = node.run()

    assert retried.returncode == 0, retried.stderr
    assert node.actions() == [["try-restart", "homelab-pi-sd.service"]]
    assert node.applied()["result"]["ok"] is True
