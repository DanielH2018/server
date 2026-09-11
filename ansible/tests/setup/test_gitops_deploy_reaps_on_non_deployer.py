#!/usr/bin/env python3
"""daniel-server ran a live gitops-deploy.timer with has_gitops: false (spec 2026-09-06).

The role was gated in initial_setup.yml, so a false flag skipped it and reaped nothing. The
role is now a dispatcher; this pins the three parts that make the false branch reachable.
Run: uv run pytest ansible/tests/setup/test_gitops_deploy_reaps_on_non_deployer.py
"""

from _helpers import ANSIBLE, load_tasks, load_yaml, task_named

ROLE = ANSIBLE / "roles" / "setup" / "gitops_deploy" / "tasks"

_UNIT_DIR = "/etc/systemd/system/"


def _systemd_units_install_writes() -> set[str]:
    """Every unit basename `install.yml` writes under `/etc/systemd/system/`.

    Reads every `template`/`copy` task's `dest:`, expanding a bare `{{ item }}` over that
    task's own `loop:` -- the shape every systemd-unit-installing task in this role uses.
    """
    units: set[str] = set()
    for task in load_tasks(ROLE / "install.yml"):
        for module_key in ("ansible.builtin.template", "ansible.builtin.copy"):
            module = task.get(module_key)
            if not isinstance(module, dict):
                continue
            dest = str(module.get("dest", ""))
            if not dest.startswith(_UNIT_DIR):
                continue
            name = dest.removeprefix(_UNIT_DIR)
            if name == "{{ item }}":
                units.update(task.get("loop", []))
            else:
                units.add(name)
    return units


def test_the_units_census_is_not_vacuous():
    """Guard against the discovery matcher silently finding nothing, or finding a subset --
    a vacuous census would make the coverage assertion below pass by comparing against
    itself."""
    units = _systemd_units_install_writes()
    assert len(units) >= 6, units
    assert {"gitops-deploy.timer", "staging-backfill.timer"} <= units


def test_the_teardown_removes_every_unit_install_writes():
    units = _systemd_units_install_writes()
    removal = task_named(
        load_tasks(ROLE / "teardown.yml"), "Remove the deployer unit files"
    )
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert units <= set(removal["loop"])


def test_the_teardown_stops_both_timers_before_removing_them():
    stat = task_named(
        load_tasks(ROLE / "teardown.yml"),
        "Check whether the GitOps deploy timers were ever installed here",
    )
    assert set(stat["loop"]) == {"gitops-deploy.timer", "staging-backfill.timer"}

    stop = task_named(
        load_tasks(ROLE / "teardown.yml"), "Stop and disable the GitOps deploy timers"
    )
    unit = stop["ansible.builtin.systemd"]
    assert unit["enabled"] is False
    assert unit["state"] == "stopped"
    assert stop["when"] == "item.stat.exists"


def test_the_teardown_reaps_both_crons_by_cron_file():
    installed = {
        t["ansible.builtin.cron"]["cron_file"]
        for t in load_tasks(ROLE / "install.yml")
        if "ansible.builtin.cron" in t
    }
    assert installed == {"github-ruleset-drift", "github-interaction-limit"}

    removed = {
        t["ansible.builtin.cron"]["cron_file"]
        for t in load_tasks(ROLE / "teardown.yml")
        if "ansible.builtin.cron" in t
        and t["ansible.builtin.cron"].get("state") == "absent"
    }
    assert removed == installed


def _directories_install_creates() -> set[str]:
    """Every path `install.yml`'s directory-creating file task loops over."""
    dirs: set[str] = set()
    for task in load_tasks(ROLE / "install.yml"):
        module = task.get("ansible.builtin.file")
        if not isinstance(module, dict) or module.get("state") != "directory":
            continue
        if module.get("path") == "{{ item }}":
            dirs.update(task.get("loop", []))
        else:
            dirs.add(module["path"])
    return dirs


def test_the_directory_census_is_not_vacuous():
    """A renamed create task, or one that stopped looping, must fail here rather than empty
    the coverage assertion below into a comparison against nothing."""
    dirs = _directories_install_creates()
    assert {
        "/opt/gitops-deploy",
        "/var/lib/gitops-deploy",
        "/etc/gitops-deploy",
    } <= dirs


def test_the_teardown_removes_every_directory_install_creates():
    """#1732: the teardown reaped units and crons and left all three directories — the stale
    payload, the 0600 config env file and the state — on daniel-server."""
    removal = task_named(
        load_tasks(ROLE / "teardown.yml"),
        "Remove the deployer's payload, config and state directories",
    )
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert removal.get("become") is True, "the directories are root-created"
    assert _directories_install_creates() <= set(removal["loop"])


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
