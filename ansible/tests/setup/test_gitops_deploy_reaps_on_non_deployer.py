#!/usr/bin/env python3
"""daniel-server ran a live gitops-deploy.timer with has_gitops: false (spec 2026-09-06).

The role was gated in initial_setup.yml, so a false flag skipped it and reaped nothing. The
role is now a dispatcher; this pins the three parts that make the false branch reachable.
Run: uv run pytest ansible/tests/setup/test_gitops_deploy_reaps_on_non_deployer.py
"""

from _helpers import ANSIBLE, load_tasks, load_yaml, task_named

ROLE = ANSIBLE / "roles" / "setup" / "gitops_deploy" / "tasks"


def _units_install_writes() -> set[str]:
    task = task_named(load_tasks(ROLE / "install.yml"), "Install systemd units")
    return set(task["loop"])


def test_the_teardown_removes_every_unit_install_writes():
    absent = {
        t["ansible.builtin.file"]["path"].rsplit("/", 1)[-1]
        for t in load_tasks(ROLE / "teardown.yml")
        if "ansible.builtin.file" in t
        and t["ansible.builtin.file"].get("state") == "absent"
    }
    # The loop item is `{{ item }}`; the path list is the loop on the removal task.
    removal = task_named(
        load_tasks(ROLE / "teardown.yml"), "Remove the deployer unit files"
    )
    assert set(removal["loop"]) == _units_install_writes() >= {"gitops-deploy.timer"}
    assert absent  # the removal task exists and is a file: absent task


def test_the_teardown_stops_the_timer_before_removing_it():
    stop = task_named(
        load_tasks(ROLE / "teardown.yml"), "Stop and disable the GitOps deploy timer"
    )
    unit = stop["ansible.builtin.systemd"]
    assert unit == {"name": "gitops-deploy.timer", "enabled": False, "state": "stopped"}


def test_the_playbook_no_longer_gates_the_role():
    """The whole bug: a playbook-level `when: has_gitops` means the false branch never runs."""
    plays = load_yaml(ANSIBLE / "initial_setup.yml")
    entries = [
        r
        for p in plays
        for r in p.get("roles", [])
        if isinstance(r, dict) and r.get("role") == "gitops_deploy"
    ]
    assert len(entries) == 1
    assert "when" not in entries[0], entries[0]


def test_main_is_a_dispatcher_over_has_gitops():
    whens = {t.get("when") for t in load_tasks(ROLE / "main.yml")}
    assert whens == {"has_gitops", "not has_gitops"}
