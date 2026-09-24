"""The retired staging-backfill ratchet is reaped from the deployer, not only from other hosts.

`teardown.yml` runs only where `has_gitops` is false, so it never reaches daniel-box, the one
host that ran the ratchet. Deleting the templates from `install.yml` alone would have left the
timer ticking there from its last rendered unit file, driving a harness that no longer exists.
These tests pin the tasks in `install.yml` that converge daniel-box (#2414).

Run: uv run pytest ansible/tests/staging/test_staging_backfill_retired.py
"""

from _helpers import SETUP_ROLES, load_tasks, task_named

ROLE = SETUP_ROLES / "gitops_deploy"
INSTALL = ROLE / "tasks" / "install.yml"

UNITS = {
    "/etc/systemd/system/staging-backfill.service",
    "/etc/systemd/system/staging-backfill.timer",
    "/etc/systemd/system/staging-backfill-alert.service",
}
LIVENESS_STATE = {
    "/var/lib/gitops-deploy/staging-backfill-armed",
    "/var/lib/gitops-deploy/staging-backfill-last-run",
}
KEPT_LEDGER = "/var/lib/gitops-deploy/staging-backfill.jsonl"

REMOVE = "Remove the retired staging-backfill units and their liveness state"
STOP_TIMER = "Stop and disable the retired staging-backfill timer"
STOP_SERVICE = "Stop the retired staging-backfill service"


def _names() -> list[str]:
    return [t.get("name", "") for t in load_tasks(INSTALL)]


def test_install_removes_every_unit_and_liveness_file():
    removal = task_named(load_tasks(INSTALL), REMOVE)
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert set(removal["loop"]) == UNITS | LIVENESS_STATE


def test_the_part_1_ledger_is_kept():
    """docs/staging-phase-c.md keeps the ledger as the raw evidence behind Part 1: MET."""
    removal = task_named(load_tasks(INSTALL), REMOVE)
    assert KEPT_LEDGER not in removal["loop"]


def test_the_timer_and_service_stop_before_their_files_go():
    """The service's ExecStopPost= rewrote the heartbeat on every exit, so a run still in
    flight when the file removal ran would recreate a file this task had just deleted."""
    names = _names()
    assert names.index(STOP_TIMER) < names.index(STOP_SERVICE) < names.index(REMOVE)

    timer = task_named(load_tasks(INSTALL), STOP_TIMER)["ansible.builtin.systemd"]
    assert timer["name"] == "staging-backfill.timer"
    assert timer["enabled"] is False and timer["state"] == "stopped"


def test_nothing_installs_a_staging_backfill_unit_any_more():
    """The rejecting half: a template task writing one of these units would re-arm the ratchet
    on the next apply, while the removal task above deleted it again on the same run."""
    for task in load_tasks(INSTALL):
        module = task.get("ansible.builtin.template") or task.get(
            "ansible.builtin.copy"
        )
        if not isinstance(module, dict):
            continue
        written = {str(module.get("dest", ""))} | {
            f"/etc/systemd/system/{item}" for item in task.get("loop", [])
        }
        assert not written & UNITS, task.get("name")
    assert not list((ROLE / "templates").glob("staging-backfill*"))
