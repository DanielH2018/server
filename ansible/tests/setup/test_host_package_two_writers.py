"""No path has two writers: an Ansible setup role and the host package of the same host.

The host handoff rule (`docs/target-architecture/host/h3-pi-pilot.md`, *The host handoff rule*)
gives every path one owner. The PR that moves an item into a package deletes the Ansible task
that wrote it, in the same commit. A task left behind rewrites the packaged file on every
apply, and the next package upgrade writes it back: each side undoes the other, and neither
reports a failure.

The census joins two sides per host:

- **Ansible.** Every setup role's tasks, through `_role_census.task_files_by_role` and
  `_helpers.walk_tasks`. A writing module contributes its `dest:` or `path:`, a cron its
  `(user, name)` and its `cron_file:`, which lands as the path `/etc/cron.d/<name>` when
  relative, so a package shipping that file meets it in the same join. A task whose `state:` renders `absent` is an absent
  arm, which removes rather than writes, so it contributes nothing. Each value renders with
  the role's context for that host (`lib.render_context`), its loop items included when the
  loop is a literal list or one variable. A value that still does not render is skipped: one
  reading a registered result, a `common` task file's caller `vars:` (`alert_unit.yml`,
  `kuma_check_timer.yml`), or a loop over a registered list. On 2026-10-13 that skipped 147
  of 1353 `dest:` and `path:` fields across the three hosts; most name a home directory or
  `/etc/systemd/system/`, where no package installs.
- **Packages.** Every host package under `hosts/<host>/` joins that host, and every shared
  package under `hosts/packages/` joins every host. Each contributes the paths it installs,
  and a host package also contributes its legacy entries, rendered as its manifest lists them.
  Reconcile retires a legacy entry, so a role still writing it is the same two-writer fault:
  each run of either side undoes the other. A `path` entry keys as that path, a
  `cron_file` as `/etc/cron.d/<name>`, a `unit` as `/etc/systemd/system/<unit>`, the file
  `homelab-reconcile:retire_unit` removes, and a `cron` as its `(user, name)`.

A role reaches the hosts `land_reach.setup_role_hosts` names. A role that function cannot
place, such as `common`, whose task files other roles import, reaches every host.

Run: uv run pytest ansible/tests/setup/test_host_package_two_writers.py
"""

import re
from collections import defaultdict
from functools import cache
from pathlib import Path, PurePosixPath

import jinja2
import pytest
from _helpers import load_tasks, walk_tasks
from _role_census import task_files_by_role
from deploy_tools.land_lib.land_reach import setup_role_hosts
from deploy_tools.setup_lib.setup_gates import HOSTS
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.render_context import render_context
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode

SETUP = ANSIBLE / "roles" / "setup"

# The modules that write the file their `dest:` or `path:` names. Named rather than read from
# every module carrying a `path:`, because `stat`, `slurp`, `acl` and `mount` read or
# annotate a path without writing it.
WRITERS = frozenset(
    f"ansible.builtin.{name}"
    for name in (
        "blockinfile",
        "copy",
        "file",
        "get_url",
        "lineinfile",
        "replace",
        "template",
        "unarchive",
    )
)
CRON = "ansible.builtin.cron"
# Where the cron module writes a relative `cron_file:`. An absolute one replaces it on join.
CRON_D = PurePosixPath("/etc/cron.d")

# The Ansible-side members: the interim push-token files optimize_pi stages on the Pi, which
# the package reads through LoadCredential= and never writes (H3.8a).
OPTIMIZE_PI_WRITES = frozenset(
    {
        ("path", "/etc/homelab/interim/pi_sd_health_push_token"),
        ("path", "/etc/homelab/interim/pi_recovery_push_token"),
    }
)
# The legacy members: the three Pi heartbeat crons PR H3.8b retires, which the package side
# must hold, so a role scheduling one again meets it in the join.
HEARTBEAT_CRONS = frozenset(
    {
        "Pi SD-card health heartbeat",
        "Pi rotated-log integrity sweep",
        "Pi container-recovery heartbeat",
    }
)
# The package-side members: the shared package, and the Pi's host package from PR H3.6.
NAMED_PACKAGES = frozenset({"homelab-updater", "homelab-daniel-pi"})

_ONE_VARIABLE = re.compile(r"^\{\{\s*([A-Za-z_][\w.]*)\s*\}\}$")

_ENV = make_ansible_env(undefined_cls=jinja2.StrictUndefined)

# A write: ("path", "/abs/path") or ("cron", "<user>", "<name>"). A cron's `cron_file:` is a
# path, so it joins a package's installed files. The `(user, name)` key can meet only a legacy
# entry.
Key = tuple[str, ...]
# Where an Ansible-era unit a legacy entry names lives.
ETC_UNITS = PurePosixPath("/etc/systemd/system")


@cache
def _role_context(role: Path, host: str) -> dict:
    return render_context(role, host)


def _render(value: object, ctx: dict) -> str | None:
    """`value` rendered with `ctx`, or None when it reads a name the context lacks."""
    try:
        return _ENV.from_string(str(value)).render(ctx)
    except jinja2.TemplateError:
        return None


def _loop_items(task: dict, ctx: dict) -> list:
    """The items a task's loop runs over, `[None]` for no loop, `[]` when they won't resolve."""
    loop = task.get("loop", task.get("with_items"))
    if loop is None:
        return [None]
    if isinstance(loop, list):
        return loop
    match = _ONE_VARIABLE.match(str(loop))
    if match:
        try:
            items = _ENV.compile_expression(match.group(1))(**ctx)
        except jinja2.TemplateError:
            return []
        if isinstance(items, list):
            return items
    return []


def task_writes(task: dict, ctx: dict) -> set[Key]:
    """Every path, cron and cron file one task writes, rendered with `ctx`."""
    writes: set[Key] = set()
    for module, args in task.items():
        if module not in WRITERS | {CRON} or not isinstance(args, dict):
            continue
        for item in _loop_items(task, ctx):
            item_ctx = ctx if item is None else {**ctx, "item": item}
            if _render(args.get("state", ""), item_ctx) == "absent":
                continue
            fields = {
                key: rendered
                for key, value in args.items()
                if key in {"dest", "path", "name", "user", "cron_file"}
                and (rendered := _render(value, item_ctx))
            }
            if module == CRON:
                if "cron_file" in fields:
                    writes.add(("path", str(CRON_D / fields["cron_file"])))
                if "name" in fields:
                    writes.add(("cron", fields.get("user", "root"), fields["name"]))
                continue
            writes.update(
                ("path", fields[key]) for key in ("dest", "path") if key in fields
            )
    return writes


def ansible_writes(
    roles: list[tuple[Path, Path]], hosts: tuple[str, ...]
) -> dict[str, dict[Key, set[str]]]:
    """Per host, each write the setup roles make there, mapped to the task files making it."""
    by_host: dict[str, dict[Key, set[str]]] = defaultdict(lambda: defaultdict(set))
    for role, task_file in roles:
        reach = setup_role_hosts(role.name) or frozenset(hosts)
        tasks = list(walk_tasks(load_tasks(task_file)))
        for host in sorted(reach & set(hosts)):
            ctx = _role_context(role, host)
            for task in tasks:
                for key in task_writes(task, ctx):
                    by_host[host][key].add(str(task_file.relative_to(ANSIBLE.parent)))
    return by_host


def legacy_key(entry: dict[str, str]) -> Key:
    """The census key of the write a legacy entry retires."""
    if "cron" in entry:
        return ("cron", entry["user"], entry["cron"])
    if "cron_file" in entry:
        return ("path", str(CRON_D / entry["cron_file"]))
    if "unit" in entry:
        return ("path", str(ETC_UNITS / entry["unit"]))
    return ("path", entry["path"])


def package_writes(
    hosts_root: Path, hosts: tuple[str, ...]
) -> dict[str, dict[Key, set[str]]]:
    """Per host, each path its packages install or retire, mapped to the packages doing so.

    A retiring package is named `<package> (legacy)`, so a collision line says which side the
    package is on.
    """
    by_host: dict[str, dict[Key, set[str]]] = defaultdict(lambda: defaultdict(set))
    shared = [
        host_mode.render_package(spec.parent.name, hosts_root=hosts_root)
        for spec in sorted((hosts_root / "packages").glob("*/package.yml"))
    ]
    for host in hosts:
        packages = list(shared)
        if (hosts_root / host / "package.yml").is_file():
            packages.append(host_mode.render_host(host, hosts_root=hosts_root))
        for package in packages:
            for dst in package.files:
                by_host[host][("path", dst)].add(package.name)
            for entry in package.legacy:
                by_host[host][legacy_key(entry)].add(f"{package.name} (legacy)")
    return by_host


def collisions(
    ansible: dict[str, dict[Key, set[str]]], packages: dict[str, dict[Key, set[str]]]
) -> list[str]:
    """One line per write both sides make on the same host."""
    return [
        f"{host}: {' '.join(key)} is written by {sorted(ansible[host][key])} "
        f"and shipped by {sorted(packages[host][key])}"
        for host in sorted(ansible.keys() & packages.keys())
        for key in sorted(ansible[host].keys() & packages[host].keys())
    ]


@cache
def _tree() -> tuple[dict, dict]:
    return (
        ansible_writes(task_files_by_role(SETUP), HOSTS),
        package_writes(host_mode.HOSTS, HOSTS),
    )


def test_no_path_has_two_writers():
    ansible, packages = _tree()
    found = collisions(ansible, packages)
    assert not found, "\n".join(found)


def test_the_ansible_side_finds_the_optimize_pi_interim_files():
    ansible, _ = _tree()
    assert OPTIMIZE_PI_WRITES <= set(ansible["daniel-pi"])


def test_the_package_side_finds_the_retired_heartbeat_crons():
    _, packages = _tree()
    names = {key[2] for key in packages["daniel-pi"] if key[0] == "cron"}
    assert HEARTBEAT_CRONS <= names


def test_the_package_side_finds_its_named_packages():
    _, packages = _tree()
    shipped = {name for writers in packages["daniel-pi"].values() for name in writers}
    assert NAMED_PACKAGES <= shipped


# ── the red proof: a fixture role still writing a path a fixture package ships ────────

SD_HEALTH = "/usr/lib/homelab/pi-sd-health"


def _fixture(
    tmp_path: Path, task: str, shipped: str = SD_HEALTH, legacy: str = ""
) -> tuple[dict, dict]:
    """A fixture `optimize_pi` running `task`, and a daniel-pi package shipping `shipped`.

    `legacy` is the YAML of the package's `legacy:` list, empty for none.
    """
    role = tmp_path / "roles" / "setup" / "optimize_pi"
    (role / "tasks").mkdir(parents=True)
    task_file = role / "tasks" / "main.yml"
    task_file.write_text(task)
    hosts = tmp_path / "hosts"
    (hosts / "packages").mkdir(parents=True)
    (hosts / "daniel-pi").mkdir()
    (hosts / "daniel-pi" / "pi-sd-health").write_text("#!/bin/sh\n")
    (hosts / "daniel-pi" / "package.yml").write_text(
        f"files:\n  - {{src: pi-sd-health, dst: {shipped}, mode: '0755'}}\n"
        + (f"legacy:\n{legacy}" if legacy else "")
    )
    ansible: dict = {"daniel-pi": defaultdict(set)}
    ctx = _role_context(SETUP / "optimize_pi", "daniel-pi")
    for entry in walk_tasks(load_tasks(task_file)):
        for key in task_writes(entry, ctx):
            ansible["daniel-pi"][key].add("optimize_pi")
    return ansible, package_writes(hosts, ("daniel-pi",))


def test_a_role_still_writing_a_packaged_path_is_flagged(tmp_path):
    task = (
        "- name: Deploy the SD-card health script\n"
        "  ansible.builtin.template:\n"
        "    src: pi-sd-health.sh.j2\n"
        "    dest: /usr/lib/homelab/{{ item }}\n"
        "  loop: [pi-sd-health]\n"
    )
    found = collisions(*_fixture(tmp_path, task))
    assert found == [
        f"daniel-pi: path {SD_HEALTH} is written by ['optimize_pi'] "
        "and shipped by ['homelab-daniel-pi']"
    ]


def test_a_role_cron_file_a_package_ships_is_flagged(tmp_path):
    task = (
        "- name: Pi SD-card health heartbeat\n"
        "  ansible.builtin.cron:\n"
        "    name: Pi SD-card health heartbeat\n"
        "    user: root\n"
        "    cron_file: pi-health\n"
        "    job: /usr/lib/homelab/pi-sd-health\n"
    )
    found = collisions(*_fixture(tmp_path, task, shipped="/etc/cron.d/pi-health"))
    assert found == [
        "daniel-pi: path /etc/cron.d/pi-health is written by ['optimize_pi'] "
        "and shipped by ['homelab-daniel-pi']"
    ]


HEARTBEAT_CRON = (
    "- name: Schedule the SD-card health heartbeat\n"
    "  ansible.builtin.cron:\n"
    "    name: Pi SD-card health heartbeat\n"
    "    user: '{{ sys_user }}'\n"
    "    job: /usr/local/bin/pi-sd-health.sh\n"
)


def test_a_role_still_installing_a_legacy_cron_is_flagged(tmp_path):
    # H3.8b's entry: the package retires the cron while optimize_pi still schedules it.
    legacy = "  - {cron: Pi SD-card health heartbeat, user: '{{ sys_user }}'}\n"
    found = collisions(*_fixture(tmp_path, HEARTBEAT_CRON, legacy=legacy))
    user = _role_context(SETUP / "optimize_pi", "daniel-pi")["sys_user"]
    assert found == [
        f"daniel-pi: cron {user} Pi SD-card health heartbeat is written by "
        "['optimize_pi'] and shipped by ['homelab-daniel-pi (legacy)']"
    ]


@pytest.mark.parametrize(
    ("task", "legacy", "key"),
    [
        pytest.param(
            "- ansible.builtin.template:\n"
            "    src: pi-sd-health.sh.j2\n"
            "    dest: /usr/local/bin/pi-sd-health.sh\n",
            "  - {path: /usr/local/bin/pi-sd-health.sh}\n",
            "path /usr/local/bin/pi-sd-health.sh",
            id="path",
        ),
        pytest.param(
            "- ansible.builtin.cron:\n"
            "    name: Pi SD-card health heartbeat\n"
            "    cron_file: pi-health\n"
            "    user: root\n"
            "    job: /usr/local/bin/pi-sd-health.sh\n",
            "  - {cron_file: pi-health}\n",
            "path /etc/cron.d/pi-health",
            id="cron_file",
        ),
        pytest.param(
            "- ansible.builtin.template:\n"
            "    src: pi-sd-health.service.j2\n"
            "    dest: /etc/systemd/system/pi-sd-health.service\n",
            "  - {unit: pi-sd-health.service}\n",
            "path /etc/systemd/system/pi-sd-health.service",
            id="unit",
        ),
    ],
)
def test_a_role_still_writing_a_legacy_path_is_flagged(tmp_path, task, legacy, key):
    found = collisions(*_fixture(tmp_path, task, legacy=legacy))
    assert found == [
        f"daniel-pi: {key} is written by ['optimize_pi'] "
        "and shipped by ['homelab-daniel-pi (legacy)']"
    ]


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(
            "- name: Retire the SD-card health script\n"
            "  ansible.builtin.file:\n"
            f"    path: {SD_HEALTH}\n"
            "    state: \"{{ 'absent' if true else 'file' }}\"\n",
            id="absent-arm",
        ),
        pytest.param(
            "- name: Read the SD-card health script\n"
            "  ansible.builtin.stat:\n"
            f"    path: {SD_HEALTH}\n",
            id="read-only-module",
        ),
    ],
)
def test_a_role_that_only_removes_or_reads_the_path_is_clean(tmp_path, task):
    assert collisions(*_fixture(tmp_path, task)) == []


def test_a_cron_contributes_its_user_and_name_and_its_cron_file():
    task = yaml_fast.safe_load(
        "- ansible.builtin.cron:\n"
        "    name: Pi SD-card health heartbeat\n"
        "    user: '{{ sys_user }}'\n"
        "    cron_file: pi-health\n"
    )[0]
    ctx = _role_context(SETUP / "optimize_pi", "daniel-pi")
    assert task_writes(task, ctx) == {
        ("cron", ctx["sys_user"], "Pi SD-card health heartbeat"),
        ("path", "/etc/cron.d/pi-health"),
    }
