"""No ufw rule has two writers: an Ansible `community.general.ufw` task and a host package (H7.2b).

H3's census (`test_host_package_two_writers.py`) joins paths and legacy entries. A ufw rule is not
a path, so this census joins rules. For each host it reads two sides:

- **Ansible.** Every setup role's `community.general.ufw` task that applies to the host, with its
  arguments rendered for that host and canonicalised into one tuple (`rule`, `route`,
  `direction`, `proto`, source and port, destination and port). A task with `delete: true` is an
  absent arm and writes nothing. A task, block or import guarded by `<role>_packaged_hosts`
  writes nothing on the hosts the map lists (`setup_gates.packaged_guard_hosts`).
- **Package.** The host package's `ufw:` rules and legacy rules, parsed from the spec text the
  way `ufw show added` prints it and canonicalised the same way. A legacy rule is one reconcile
  deletes, so a role still adding it is the same fault.

A tuple on both sides for one host is a collision: each Ansible run adds the rule and each
reconcile run fights it.

Run: uv run pytest ansible/tests/setup/test_host_package_ufw_two_writers.py
"""

import os
import re
from collections import defaultdict
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest
from _role_census import task_files_by_role
from _task_guards import guarded_tasks
from deploy_tools.land_lib.land_reach import setup_role_hosts
from deploy_tools.setup_lib.setup_gates import (
    HOSTS,
    GateUnreadable,
    packaged_guard_hosts,
)
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode
from test_host_package_two_writers import (
    _loop_items,
    _render,
    _role_context,
    ansible_writes,
    collisions,
    package_writes,
)

SETUP = ANSIBLE / "roles" / "setup"
MODULES = ("community.general.ufw", "ufw")
SERVICES = {"ssh": "22", "http": "80", "https": "443"}
RULES = frozenset({"allow", "deny", "limit", "reject"})
KEYWORDS = frozenset({"from", "to", "port", "proto"})
# One canonical rule: (route, rule, direction, proto, from, from port, to, to port).
Rule = tuple[str, str, str, str, str, str, str, str]
LAN = "192.0.2.0/24"


def _port(value: object) -> str:
    return SERVICES.get(str(value), str(value))


def canonical(
    rule: str,
    *,
    route: bool = False,
    direction: str = "in",
    proto: str = "any",
    src: str = "any",
    sport: str = "",
    dst: str = "any",
    dport: str = "",
) -> Rule:
    return (
        "route" if route else "",
        rule,
        direction,
        proto,
        src or "any",
        _port(sport) if sport else "",
        dst or "any",
        _port(dport) if dport else "",
    )


def from_args(args: dict, render: Callable[[object], str | None]) -> Rule | None:
    """The rule one ufw task adds, or None for a delete or a task that sets no rule."""
    values = {key: render(value) for key, value in args.items()}
    if values.get("delete") in ("true", "True") or values.get("rule") not in RULES:
        return None

    def first(*names: str) -> str:
        return next((values[n] for n in names if values.get(n)), "") or ""

    return canonical(
        values["rule"] or "",
        route=values.get("route") in ("true", "True"),
        direction=first("direction") or "in",
        proto=first("proto") or "any",
        src=first("from_ip", "src", "from"),
        sport=first("from_port"),
        dst=first("to_ip", "dest", "to"),
        dport=first("to_port", "port"),
    )


def from_spec(spec: str) -> Rule:
    """A package's rule text, as `ufw show added` prints it after `ufw `, canonicalised."""
    words = spec.split()
    rule, words = words[0], words[1:]
    route = rule == "route"
    if route:
        rule, words = words[0], words[1:]
    direction = "in"
    if words and words[0] in ("in", "out"):
        direction, words = words[0], words[1:]
    found = {"proto": "any", "src": "any", "sport": "", "dst": "any", "dport": ""}
    if (
        words
        and words[0] not in KEYWORDS
        and re.fullmatch(r"\w+(/(tcp|udp))?", words[0])
    ):
        port, _, proto = words[0].partition("/")
        found.update(dport=port, proto=proto or "any")
        words = []
    side = "dst"
    for keyword, value in zip(words[::2], words[1::2], strict=False):
        if keyword == "from":
            found["src"], side = value, "src"
        elif keyword == "to":
            found["dst"], side = value, "dst"
        elif keyword == "port":
            found["sport" if side == "src" else "dport"] = value
        elif keyword == "proto":
            found["proto"] = value
    return canonical(rule, route=route, direction=direction, **found)


def ufw_writes(
    roles: list[tuple[Path, Path]],
    hosts: tuple[str, ...],
    reach_of: Callable[[str], frozenset[str]] = setup_role_hosts,
) -> dict[str, dict[Rule, set[str]]]:
    """Per host, each ufw rule the setup roles add there, mapped to the tasks adding it."""
    by_host: dict[str, dict[Rule, set[str]]] = defaultdict(lambda: defaultdict(set))
    for role, task_file in roles:
        reach = reach_of(role.name) or frozenset(hosts)
        tasks = list(guarded_tasks(role, task_file))
        for host in sorted(reach & set(hosts)):
            ctx = _role_context(role, host)
            for task, whens in tasks:
                args = next(
                    (task[m] for m in MODULES if isinstance(task.get(m), dict)), None
                )
                if args is None or packaged_guard_hosts(whens, [host], role):
                    continue
                for item in _loop_items(task, ctx):
                    item_ctx = ctx if item is None else {**ctx, "item": item}
                    rule = from_args(args, lambda v, c=item_ctx: _render(v, c))
                    if rule:
                        where = f"{os.path.relpath(task_file, ANSIBLE.parent)}: {task.get('name', '?')}"
                        by_host[host][rule].add(where)
    return by_host


def ufw_package_rules(
    hosts_root: Path, hosts: tuple[str, ...]
) -> dict[str, dict[Rule, set[str]]]:
    """Per host, each rule its host package declares or retires, mapped to the package."""
    by_host: dict[str, dict[Rule, set[str]]] = defaultdict(lambda: defaultdict(set))
    for host in hosts:
        if not (hosts_root / host / "package.yml").is_file():
            continue
        package = host_mode.render_host(host, hosts_root=hosts_root)
        for rule in package.ufw_rules:
            by_host[host][from_spec(rule["spec"])].add(package.name)
        for rule in package.ufw_legacy:
            by_host[host][from_spec(rule["spec"])].add(f"{package.name} (legacy)")
    return by_host


def ufw_collisions(ansible: dict, packages: dict) -> list[str]:
    return [
        f"{host}: ufw {' '.join(filter(None, key))} is added by {sorted(ansible[host][key])} "
        f"and declared by {sorted(packages[host][key])}"
        for host in sorted(ansible.keys() & packages.keys())
        for key in sorted(ansible[host].keys() & packages[host].keys())
    ]


@cache
def _tree() -> tuple[dict, dict]:
    return (
        ufw_writes(task_files_by_role(SETUP), HOSTS),
        ufw_package_rules(host_mode.HOSTS, HOSTS),
    )


def test_no_ansible_ufw_task_adds_a_rule_a_host_package_declares():
    ansible, packages = _tree()
    assert not ufw_collisions(ansible, packages)


@pytest.mark.parametrize("host", HOSTS)
def test_the_ansible_side_finds_initial_setups_ssh_limit_on_every_host(host):
    ansible, _ = _tree()
    assert canonical("limit", proto="tcp", dport="22") in ansible[host]


def test_the_ansible_side_finds_the_lan_ssh_allow_with_its_subnet_rendered():
    ansible, _ = _tree()
    lan = [key for key in ansible["daniel-pi"] if key[1] == "allow" and key[7] == "22"]
    assert lan and all("{{" not in key[4] for key in lan)


# ── the canonical form: a spec and a task for one rule must meet ──────────────────────


@pytest.mark.parametrize(
    ("spec", "args"),
    [
        pytest.param(
            "limit 22/tcp",
            {"rule": "limit", "port": "ssh", "proto": "tcp"},
            id="service",
        ),
        pytest.param(
            f"allow from {LAN} to any port 22 proto tcp",
            {"rule": "allow", "port": "22", "proto": "tcp", "from_ip": LAN},
            id="from",
        ),
        pytest.param("allow 80", {"rule": "allow", "port": "80"}, id="bare-port"),
        pytest.param(
            "deny 23/udp",
            {"rule": "deny", "to_port": "23", "proto": "udp"},
            id="to_port",
        ),
        pytest.param(
            f"route allow from {LAN} to any",
            {"rule": "allow", "route": "true", "src": LAN},
            id="route",
        ),
    ],
)
def test_a_spec_and_the_task_that_adds_it_canonicalise_to_one_tuple(spec, args):
    assert from_spec(spec) == from_args(args, str)


@pytest.mark.parametrize(
    ("spec", "args"),
    [
        pytest.param(
            "limit 22/tcp", {"rule": "allow", "port": "22", "proto": "tcp"}, id="rule"
        ),
        pytest.param(
            "allow 22/tcp", {"rule": "allow", "port": "22", "proto": "udp"}, id="proto"
        ),
        pytest.param(
            "allow 22/tcp", {"rule": "allow", "port": "2222", "proto": "tcp"}, id="port"
        ),
        pytest.param(
            f"allow from {LAN} to any port 22",
            {"rule": "allow", "port": "22", "from_ip": "198.51.100.0/24"},
            id="source",
        ),
    ],
)
def test_two_different_rules_do_not_meet(spec, args):
    assert from_spec(spec) != from_args(args, str)


def test_a_delete_task_and_a_task_with_no_rule_add_nothing():
    assert from_args({"rule": "allow", "port": "22", "delete": "true"}, str) is None
    assert from_args({"default": "deny", "direction": "incoming"}, str) is None


# ── red proofs: fixture roles and packages ────────────────────────────────────────────

SSH_TASK = (
    "- name: Rate-limit ssh\n"
    "  community.general.ufw:\n"
    "    rule: limit\n"
    "    port: ssh\n"
    "    proto: tcp\n"
)
DEFAULTS = "demo_packaged_hosts:\n  ssh: [daniel-pi]\n"


def _fixture(
    tmp_path: Path,
    tasks: str,
    *,
    defaults: str = "",
    packages: tuple[str, ...] = ("daniel-pi", "daniel-server"),
    main: str | None = None,
) -> tuple[dict, dict]:
    """A role `demo` running `tasks` on every host, and a package per host in `packages`.

    Each fixture package declares `limit 22/tcp`. `main` makes `tasks` an imported file.
    """
    role = tmp_path / "roles" / "demo"
    (role / "tasks").mkdir(parents=True)
    task_file = role / "tasks" / ("ssh.yml" if main else "main.yml")
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
        (hosts / host / "package.yml").write_text(
            "ufw:\n  rules:\n    - {id: ssh-limit, spec: 'limit 22/tcp', keep: true}\n"
        )
    roles = [(role, task_file)]
    if main:
        roles.insert(0, (role, role / "tasks" / "main.yml"))
    ansible = ufw_writes(roles, HOSTS, lambda name: frozenset())
    return ansible, ufw_package_rules(hosts, HOSTS)


def test_a_role_still_adding_a_rule_a_package_declares_is_flagged_on_each_host(
    tmp_path,
):
    found = ufw_collisions(*_fixture(tmp_path, SSH_TASK))

    assert [line.split(":")[0] for line in found] == ["daniel-pi", "daniel-server"]
    assert "declared by ['homelab-daniel-pi']" in found[0]


def test_a_delete_task_for_the_declared_rule_is_clean(tmp_path):
    assert not ufw_collisions(*_fixture(tmp_path, SSH_TASK + "    delete: true\n"))


def test_a_task_guarded_by_the_map_is_clean_on_the_listed_host_and_flagged_on_the_other(
    tmp_path,
):
    guarded = (
        SSH_TASK + "  when: inventory_hostname not in demo_packaged_hosts['ssh']\n"
    )

    found = ufw_collisions(*_fixture(tmp_path, guarded, defaults=DEFAULTS))

    assert [line.split(":")[0] for line in found] == ["daniel-server"]


def test_the_same_guard_on_a_block_reads_per_host(tmp_path):
    tasks = (
        "- name: Block\n"
        "  when: inventory_hostname not in demo_packaged_hosts['ssh']\n"
        "  block:\n" + "".join("  " + line + "\n" for line in SSH_TASK.splitlines())
    )

    found = ufw_collisions(*_fixture(tmp_path, tasks, defaults=DEFAULTS))

    assert [line.split(":")[0] for line in found] == ["daniel-server"]


def test_the_same_guard_on_an_import_reads_per_host(tmp_path):
    main = (
        "- ansible.builtin.import_tasks: ssh.yml\n"
        "  when: inventory_hostname not in demo_packaged_hosts['ssh']\n"
    )

    found = ufw_collisions(*_fixture(tmp_path, SSH_TASK, defaults=DEFAULTS, main=main))

    assert [line.split(":")[0] for line in found] == ["daniel-server"]


def test_a_guard_whose_map_only_the_role_defaults_define_needs_them_loaded(tmp_path):
    guarded = (
        SSH_TASK + "  when: inventory_hostname not in demo_packaged_hosts['ssh']\n"
    )

    with pytest.raises(GateUnreadable):
        _fixture(tmp_path, guarded)  # no defaults/main.yml: the name cannot resolve


def test_a_guard_that_lists_one_host_does_not_hide_a_collision_on_another(tmp_path):
    guarded = (
        SSH_TASK + "  when: inventory_hostname not in demo_packaged_hosts['ssh']\n"
    )

    found = ufw_collisions(
        *_fixture(tmp_path, guarded, defaults=DEFAULTS, packages=("daniel-box",))
    )

    assert [line.split(":")[0] for line in found] == ["daniel-box"]


# ── H3's path census reads the same guard ─────────────────────────────────────────────


def _copy_fixture(tmp_path: Path, task: str, defaults: str) -> list[str]:
    role = tmp_path / "roles" / "demo"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text(defaults)
    (role / "tasks" / "main.yml").write_text(task)
    hosts = tmp_path / "hosts"
    (hosts / "packages").mkdir(parents=True)
    for host in ("daniel-pi", "daniel-server"):
        (hosts / host).mkdir()
        (hosts / host / "demo.conf").write_text("x\n")
        (hosts / host / "package.yml").write_text(
            "files:\n  - {src: demo.conf, dst: /etc/demo.conf, mode: '0644'}\n"
        )
    ansible = ansible_writes(
        [(role, role / "tasks" / "main.yml")], HOSTS, lambda n: frozenset()
    )
    return collisions(ansible, package_writes(hosts, HOSTS))


COPY = "- ansible.builtin.copy:\n    dest: /etc/demo.conf\n    content: x\n"
COPY_GUARDED = COPY + "  when: inventory_hostname not in demo_packaged_hosts['conf']\n"


def test_a_path_task_with_no_guard_collides_on_both_package_hosts(tmp_path):
    found = _copy_fixture(tmp_path, COPY, "demo_packaged_hosts: {conf: []}\n")

    assert [line.split(":")[0] for line in found] == ["daniel-pi", "daniel-server"]


def test_a_path_task_guarded_for_daniel_pi_collides_only_on_daniel_server(tmp_path):
    found = _copy_fixture(
        tmp_path, COPY_GUARDED, "demo_packaged_hosts: {conf: [daniel-pi]}\n"
    )

    assert [line.split(":")[0] for line in found] == ["daniel-server"]
