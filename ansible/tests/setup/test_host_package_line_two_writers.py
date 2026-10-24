"""No in-place edit has two writers: an Ansible task and a host package's line arm (H7.2d).

H3's census (`test_host_package_two_writers.py`) joins paths and legacy entries, and the ufw
census (`test_host_package_ufw_two_writers.py`) joins firewall rules. An edit in a shared file is
neither, so this census joins edits. For each host it reads two sides:

- **Ansible.** Every setup role's `lineinfile`, `blockinfile` and `ansible.posix.authorized_key`
  task that names a path, and every `ansible.posix.sysctl` task with no `sysctl_file:`, which
  edits `/etc/sysctl.conf`. A `state: absent` task removes and writes nothing. A task, block or
  import guarded by `<role>_packaged_hosts` writes nothing on the hosts the map lists
  (`setup_gates.packaged_guard_hosts`).
- **Package.** The host package's `lines:` edits.

An edit keys on its path. `/etc/sysctl.conf` is the exception: seven roles' sysctl tasks share
it and move in different PRs, so it keys on the path and the sysctl key. A task for one key does
not collide with a declaration of another, which lets zram's keys move before the hardening keys.

An edit on both sides for one host is a collision: each Ansible run rewrites the line and each
reconcile run rewrites it back.

Run: uv run pytest ansible/tests/setup/test_host_package_line_two_writers.py
"""

import os
from collections import defaultdict
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest
from _role_census import task_files_by_role
from _task_guards import guarded_tasks
from deploy_tools.land_lib.land_reach import setup_role_hosts
from deploy_tools.setup_lib.setup_gates import HOSTS, packaged_guard_hosts
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode
from test_host_package_two_writers import (
    _loop_items,
    _render,
    _role_context,
    collisions,
)

SETUP = ANSIBLE / "roles" / "setup"
SYSCTL = "/etc/sysctl.conf"
FILE_EDITS = tuple(
    f"{prefix}{name}"
    for prefix in ("ansible.builtin.", "ansible.posix.", "")
    for name in ("lineinfile", "blockinfile", "authorized_key")
)
SYSCTL_MODULES = ("ansible.posix.sysctl", "sysctl")
# One edit: ("path", "/abs/path"), or ("sysctl", "<key>") for a key of /etc/sysctl.conf.
Edit = tuple[str, str]


def edit_key(path: str, line: str = "") -> Edit:
    """The key an edit of `path` joins on; a sysctl line keys on its sysctl key."""
    if path == SYSCTL and "=" in line:
        return ("sysctl", line.partition("=")[0].strip())
    return ("path", path)


def task_edits(task: dict, ctx: dict) -> set[Edit]:
    """Every edit one task makes, rendered with `ctx`."""
    found: set[Edit] = set()
    for module in (m for m in task if m in FILE_EDITS + SYSCTL_MODULES):
        args = task[module]
        if not isinstance(args, dict):
            continue
        for item in _loop_items(task, ctx):
            item_ctx = ctx if item is None else {**ctx, "item": item}
            values = {k: _render(v, item_ctx) for k, v in args.items()}
            if values.get("state") == "absent":
                continue
            if module in SYSCTL_MODULES:
                key = values.get("name")
                if key and not values.get("sysctl_file"):
                    found.add(("sysctl", key))
                continue
            path = values.get("path") or values.get("dest")
            if path:
                found.add(edit_key(path, values.get("line") or ""))
    return found


def line_writes(
    roles: list[tuple[Path, Path]],
    hosts: tuple[str, ...],
    reach_of: Callable[[str], frozenset[str]] = setup_role_hosts,
) -> dict[str, dict[Edit, set[str]]]:
    """Per host, each edit the setup roles make there, mapped to the task files making it."""
    by_host: dict[str, dict[Edit, set[str]]] = defaultdict(lambda: defaultdict(set))
    for role, task_file in roles:
        reach = reach_of(role.name) or frozenset(hosts)
        tasks = list(guarded_tasks(role, task_file))
        for host in sorted(reach & set(hosts)):
            ctx = _role_context(role, host)
            for task, whens in tasks:
                if packaged_guard_hosts(whens, [host], role):
                    continue
                for key in task_edits(task, ctx):
                    where = os.path.relpath(task_file, ANSIBLE.parent)
                    by_host[host][key].add(where)
    return by_host


def package_lines(
    hosts_root: Path, hosts: tuple[str, ...]
) -> dict[str, dict[Edit, set[str]]]:
    """Per host, each edit its host package declares, mapped to the package."""
    by_host: dict[str, dict[Edit, set[str]]] = defaultdict(lambda: defaultdict(set))
    for host in hosts:
        if not (hosts_root / host / "package.yml").is_file():
            continue
        package = host_mode.render_host(host, hosts_root=hosts_root)
        for edit in package.lines:
            key = edit_key(edit["path"], edit.get("line", ""))
            by_host[host][key].add(package.name)
    return by_host


@cache
def _tree() -> tuple[dict, dict]:
    return (
        line_writes(task_files_by_role(SETUP), HOSTS),
        package_lines(host_mode.HOSTS, HOSTS),
    )


def test_no_ansible_edit_touches_a_line_a_host_package_declares():
    ansible, packages = _tree()
    found = collisions(ansible, packages)
    assert not found, "\n".join(found)


@pytest.mark.parametrize("host", HOSTS)
def test_the_ansible_side_finds_the_hardening_key_initial_setup_sets(host):
    ansible, _ = _tree()
    assert ("sysctl", "net.core.bpf_jit_harden") in ansible[host]


def test_the_ansible_side_finds_a_lineinfile_task_by_its_path():
    ansible, _ = _tree()
    assert ("path", "/etc/default/rkhunter") in ansible["daniel-box"]


# ── red proofs: fixture roles and packages ────────────────────────────────────────────


def _fixture(
    tmp_path: Path,
    tasks: str,
    *,
    defaults: str = "",
    packages: tuple[str, ...] = ("daniel-pi", "daniel-server"),
    edit: str = "{path: /etc/sysctl.conf, line: 'vm.swappiness=10', regexp: '^vm.swappiness'}",
    main: str | None = None,
) -> tuple[dict, dict]:
    """A role `demo` running `tasks` on every host, and a package per host in `packages`."""
    role = tmp_path / "roles" / "demo"
    (role / "tasks").mkdir(parents=True)
    task_file = role / "tasks" / ("edit.yml" if main else "main.yml")
    task_file.write_text(tasks)
    if main:
        (role / "tasks" / "main.yml").write_text(main)
    if defaults:
        (role / "defaults").mkdir()
        (role / "defaults" / "main.yml").write_text(defaults)
    hosts = tmp_path / "hosts"
    (hosts / "packages").mkdir(parents=True)
    for host in packages:
        (hosts / host).mkdir()
        (hosts / host / "package.yml").write_text(f"lines:\n  - {edit}\n")
    roles = [(role, task_file)]
    if main:
        roles.insert(0, (role, role / "tasks" / "main.yml"))
    return (
        line_writes(roles, HOSTS, lambda name: frozenset()),
        package_lines(hosts, HOSTS),
    )


SYSCTL_TASK = (
    "- name: Tune swappiness\n"
    "  ansible.posix.sysctl:\n"
    "    name: vm.swappiness\n"
    "    value: '10'\n"
)
DEFAULTS = "demo_packaged_hosts:\n  tuning: [daniel-pi]\n"
GUARD = "  when: inventory_hostname not in demo_packaged_hosts['tuning']\n"


def _hosts(found: list[str]) -> list[str]:
    return [line.split(":")[0] for line in found]


def test_a_sysctl_task_for_a_key_the_package_keeps_is_flagged_on_each_host(tmp_path):
    found = collisions(*_fixture(tmp_path, SYSCTL_TASK))

    assert _hosts(found) == ["daniel-pi", "daniel-server"]
    assert "sysctl vm.swappiness" in found[0]
    assert "shipped by ['homelab-daniel-pi']" in found[0]


def test_a_sysctl_task_for_another_key_is_clean(tmp_path):
    other = SYSCTL_TASK.replace("vm.swappiness", "net.core.bpf_jit_harden")

    assert not collisions(*_fixture(tmp_path, other))


def test_a_sysctl_task_writing_a_drop_in_file_is_clean(tmp_path):
    drop_in = SYSCTL_TASK + "    sysctl_file: /etc/sysctl.d/90-x.conf\n"

    assert not collisions(*_fixture(tmp_path, drop_in))


def test_a_lineinfile_task_on_a_path_the_package_edits_is_flagged(tmp_path):
    task = (
        "- name: Pin the zsh env\n"
        "  ansible.builtin.lineinfile:\n"
        "    path: /etc/zsh/zshenv\n"
        "    line: export X=1\n"
    )
    edit = "{path: /etc/zsh/zshenv, line: 'export X=1', regexp: '^export X='}"

    found = collisions(*_fixture(tmp_path, task, edit=edit))

    assert _hosts(found) == ["daniel-pi", "daniel-server"]
    assert "path /etc/zsh/zshenv" in found[0]


def test_a_lineinfile_line_on_sysctl_conf_keys_on_its_sysctl_key(tmp_path):
    task = (
        "- ansible.builtin.lineinfile:\n"
        "    path: /etc/sysctl.conf\n"
        "    line: vm.swappiness = 60\n"
    )

    assert _hosts(collisions(*_fixture(tmp_path, task))) == [
        "daniel-pi",
        "daniel-server",
    ]


def test_a_task_that_removes_the_edit_is_clean(tmp_path):
    absent = (
        "- ansible.builtin.lineinfile:\n"
        "    path: /etc/zsh/zshenv\n"
        "    regexp: '^export X='\n"
        "    state: absent\n"
    )
    edit = "{path: /etc/zsh/zshenv, line: 'export X=1', regexp: '^export X='}"

    assert not collisions(*_fixture(tmp_path, absent, edit=edit))


# ── the per-host guard, in the line arm's census ──────────────────────────────────────


def test_a_task_guarded_by_the_map_is_clean_on_the_listed_host_and_flagged_on_the_other(
    tmp_path,
):
    found = collisions(*_fixture(tmp_path, SYSCTL_TASK + GUARD, defaults=DEFAULTS))

    assert _hosts(found) == ["daniel-server"]


def test_the_same_guard_on_a_block_reads_per_host(tmp_path):
    tasks = f"- name: Block\n{GUARD}  block:\n" + "".join(
        "  " + line + "\n" for line in SYSCTL_TASK.splitlines()
    )

    found = collisions(*_fixture(tmp_path, tasks, defaults=DEFAULTS))

    assert _hosts(found) == ["daniel-server"]


def test_the_same_guard_on_an_import_reads_per_host(tmp_path):
    main = f"- ansible.builtin.import_tasks: edit.yml\n{GUARD}"

    found = collisions(*_fixture(tmp_path, SYSCTL_TASK, defaults=DEFAULTS, main=main))

    assert _hosts(found) == ["daniel-server"]


def test_a_guard_that_lists_one_host_does_not_hide_a_collision_on_another(tmp_path):
    found = collisions(
        *_fixture(
            tmp_path, SYSCTL_TASK + GUARD, defaults=DEFAULTS, packages=("daniel-box",)
        )
    )

    assert _hosts(found) == ["daniel-box"]
