"""Every file a setup role installs where the AIDE check pages is listed as the roles' own.

The check pages on an unpackaged unit, apt source or key, MOTD script, init script or
/usr/local/sbin entry (`PERSIST` in aide-check.sh.j2). The setup roles write units into
/etc/systemd/system, so `ROLE_WRITTEN` names them and a deploy does not page. This census reads
the roles' tasks for every destination under a paged prefix, with the loop items and the shared
kuma-check and alert tasks resolved to concrete names, and fails when one is missing from the
list. A role that adds a unit therefore has to add its name to the script in the same change.

Run: uv run pytest ansible/tests/setup/test_aide_check_role_written.py
"""

import re

import pytest
from _aide_check_run import SCRIPT
from _helpers import SETUP_ROLES, leaf_tasks, load_tasks
from _role_census import task_files_by_role
from _shell_render import render_shell_script

# The prefixes in aide-check.sh.j2's PERSIST that a role could write under.
PAGED_PREFIXES = (
    "/etc/systemd/system/",
    "/etc/apt/sources.list.d/",
    "/etc/apt/trusted.gpg.d/",
    "/etc/apt/keyrings/",
    "/etc/update-motd.d/",
    "/etc/init.d/",
    "/usr/local/sbin/",
    "/etc/rc.local",
)
WRITING_MODULES = {
    "template",
    "copy",
    "file",
    "get_url",
    "lineinfile",
    "blockinfile",
    "unarchive",
}
# Files an installer or snapd writes, which no task names. k3s-install.sh rewrites the unit and
# its environment file on every args or version change; snapd adds a mount per snap revision.
INSTALLER_WRITTEN = (
    "/etc/systemd/system/k3s.service",
    "/etc/systemd/system/k3s.service.env",
    "/etc/systemd/system/k3s-agent.service",
    "/etc/systemd/system/k3s-agent.service.env",
    "/etc/systemd/system/snap-snapd-27738.mount",
)
# Named members: a census that stops matching fails by name rather than agreeing with an empty set.
MUST_FIND = (
    "/etc/systemd/system/gitops-deploy.service",
    "/etc/systemd/system/gitops-deploy-alert.service",
    "/etc/systemd/system/coredns-host.service",
    "/etc/systemd/system/kuma-check-live-drift.timer",
    "/etc/systemd/system/kuma-check-live-drift-boot.service",
    "/etc/systemd/system/renovate-agent-land@.service",
    "/etc/systemd/system/k3s-agent.service.d/10-log-to-file.conf",
)


def role_written_pattern() -> re.Pattern[str]:
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    found = re.search(r"^ROLE_WRITTEN='(.*)'$", body, re.MULTILINE)
    assert found, "aide-check.sh no longer defines ROLE_WRITTEN"
    return re.compile(found.group(1))


def _wired(tasks: list[dict], tail: str, name_var: str) -> list[tuple[str, bool]]:
    """The `name_var` of every import of the shared task file ending in `tail`, teardowns apart."""
    names: list[tuple[str, bool]] = []
    for task in tasks:
        target = task.get("ansible.builtin.import_tasks") or task.get(
            "ansible.builtin.include_tasks"
        )
        variables = task.get("vars") or {}
        if (
            isinstance(target, str)
            and target.endswith(tail)
            and variables.get("kuma_check_state") != "absent"
        ):
            names.append(
                (
                    str(variables[name_var]),
                    bool(variables.get("alert_unit_instanced")),
                )
            )
    return names


def _loop_values(task: dict) -> list[str] | None:
    loop = task.get("loop") or task.get("with_items")
    if isinstance(loop, list) and all(isinstance(item, str) for item in loop):
        return loop
    return None


def role_destinations(task_lists: list[list[dict]]) -> set[str]:
    """Every destination under a paged prefix the tasks write, as a concrete path.

    A destination the census cannot resolve to a concrete path is kept with its `{{`, so the
    pattern cannot match it and the census fails by naming it.
    """
    found: set[str] = set()
    k3s_units: set[str] = set()
    tasks = [t for task_list in task_lists for t in leaf_tasks(task_list)]
    for task in tasks:
        variables = task.get("vars") or {}
        if "k3s_log_unit" in variables:
            k3s_units.add(str(variables["k3s_log_unit"]))
    for name, _ in _wired(
        tasks, "common/tasks/kuma_check_timer.yml", "kuma_check_name"
    ):
        found |= {
            f"/etc/systemd/system/kuma-check-{name}{suffix}"
            for suffix in (".service", ".timer", "-boot.service")
        }
    for name, instanced in _wired(
        tasks, "common/tasks/alert_unit.yml", "alert_unit_name"
    ):
        found.add(f"/etc/systemd/system/{name}-alert{'@' if instanced else ''}.service")
    for task in tasks:
        for key, args in task.items():
            if key.split(".")[-1] not in WRITING_MODULES or not isinstance(args, dict):
                continue
            if args.get("state") in {"absent", "directory", "touch"}:
                continue
            raw = args.get("dest") or args.get("path")
            if not isinstance(raw, str):
                continue
            values = [raw]
            if "{{ item }}" in raw:
                items = _loop_values(task)
                values = [raw.replace("{{ item }}", i) for i in items or []] or [raw]
            if "{{ k3s_log_unit }}" in raw:
                values = [
                    raw.replace("{{ k3s_log_unit }}", u) for u in sorted(k3s_units)
                ]
            found |= {v for v in values if v.startswith(PAGED_PREFIXES)}
    return found


def unlisted(destinations: set[str], pattern: re.Pattern[str]) -> list[str]:
    return sorted(d for d in destinations if not pattern.search(d))


# The shared tasks name their units through the caller's variables, which `role_destinations`
# resolves from each import, so the files themselves are not read.
SHARED_TASK_FILES = {"kuma_check_timer.yml", "alert_unit.yml"}


def _setup_task_lists() -> list[list[dict]]:
    return [
        load_tasks(path)
        for _, path in task_files_by_role(SETUP_ROLES)
        if path.name not in SHARED_TASK_FILES
    ]


def test_the_census_finds_the_roles_units() -> None:
    found = role_destinations(_setup_task_lists())
    assert set(MUST_FIND) <= found, sorted(set(MUST_FIND) - found)


def test_every_unit_a_role_installs_is_listed_as_role_written() -> None:
    missing = unlisted(role_destinations(_setup_task_lists()), role_written_pattern())
    assert not missing, (
        f"setup roles install {missing} where aide-check.sh pages, but ROLE_WRITTEN omits "
        "them, so the next deploy that rewrites one would push DOWN: add the names to "
        "ROLE_WRITTEN in aide-check.sh.j2"
    )


def test_the_installers_files_are_listed_as_role_written() -> None:
    assert unlisted(set(INSTALLER_WRITTEN), role_written_pattern()) == []


def test_a_unit_no_role_installs_is_not_listed() -> None:
    pattern = role_written_pattern()
    for path in (
        "/etc/systemd/system/unlisted.service",
        "/etc/systemd/system/gitops-deploy.service.d/override.conf",
        "/etc/apt/sources.list.d/extra.list",
        "/usr/local/sbin/helper",
    ):
        assert unlisted({path}, pattern) == [path]


@pytest.mark.parametrize(
    "dest",
    [
        "/etc/systemd/system/new-role.service",
        "/etc/apt/sources.list.d/new-role.list",
    ],
)
def test_a_role_that_installs_an_unlisted_path_is_reported(dest: str) -> None:
    """The census must be able to go red: a fixture task for a path the list omits."""
    fixture = [{"ansible.builtin.template": {"src": "x.j2", "dest": dest}}]
    assert unlisted(role_destinations([fixture]), role_written_pattern()) == [dest]


def test_a_loop_item_destination_is_resolved_to_each_name() -> None:
    fixture = [
        {
            "ansible.builtin.template": {
                "src": "{{ item }}.j2",
                "dest": "/etc/systemd/system/{{ item }}",
            },
            "loop": ["a.service", "b.timer"],
        }
    ]
    assert role_destinations([fixture]) == {
        "/etc/systemd/system/a.service",
        "/etc/systemd/system/b.timer",
    }
