#!/usr/bin/env python3
"""The homelab_builder role's repository half (H3.4b) and the teardown that reaps it.

The plan is docs/target-architecture/host/h3-pi-pilot.md, *Steps, as PRs*, item 5. Each test
pins one task the plan names: the users, their directories, the signing key, the apt
repository unit and its ufw rule. The teardown tests hold the disarm's two promises: it reaps
everything install.yml writes outside the signer's tree, and it never touches that tree, which
holds the key every host trusts.

Run: uv run pytest ansible/tests/setup/test_homelab_builder.py
"""

from _helpers import SETUP_ROLES, load_defaults, load_tasks, task_named, walk_tasks
from _setup_render import render_setup_text

ROLE = SETUP_ROLES / "homelab_builder"
INSTALL = load_tasks(ROLE / "tasks" / "install.yml")
# install.yml imports builder.yml, H3.5a's builder half, so the armed arm is both files.
ARMED = INSTALL + load_tasks(ROLE / "tasks" / "builder.yml")
TEARDOWN = load_tasks(ROLE / "tasks" / "teardown.yml")
DEFAULTS = load_defaults(ROLE)

_UNIT_DIR = "/etc/systemd/system/"
_SIGNER_MARKERS = (
    "homelab_builder_signer_dir",
    "/var/lib/homelab-signer",
    "homelab-signer",
)


def _strings(node):
    """Every string inside a parsed task, keys and values both."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def signer_naming_tasks(tasks) -> list[str]:
    """Names of the tasks that name the signer's directory, its variable or its user."""
    return [
        str(task.get("name"))
        for task in walk_tasks(tasks)
        if any(m in s for s in _strings(task) for m in _SIGNER_MARKERS)
    ]


def _units_install_writes() -> set[str]:
    units = set()
    for task in walk_tasks(ARMED):
        template = task.get("ansible.builtin.template") or {}
        dest = str(template.get("dest", ""))
        if not dest.startswith(_UNIT_DIR):
            continue
        if "{{ item }}" in dest:
            units.update(task["loop"])
        else:
            units.add(dest.removeprefix(_UNIT_DIR))
    # The alert unit comes from common/tasks/alert_unit.yml, under the import's name.
    for task in walk_tasks(ARMED):
        name = (task.get("vars") or {}).get("alert_unit_name")
        if name and "alert_unit.yml" in str(task.get("ansible.builtin.import_tasks")):
            units.add(f"{name}-alert.service")
    return units


def test_the_builder_is_armed():
    assert DEFAULTS["homelab_builder_enabled"] is True


def test_install_creates_both_users_as_system_accounts_without_a_home():
    task = task_named(INSTALL, "Create the builder and signer system users")
    module = task["ansible.builtin.user"]
    assert module["system"] is True
    assert module["create_home"] is False
    assert module["shell"] == "/usr/sbin/nologin"
    assert {u["name"] for u in task["loop"]} == {"homelab-builder", "homelab-signer"}


def test_the_state_directories_carry_the_plan_owners_and_modes():
    task = task_named(INSTALL, "Create the builder and signer state directories")
    got = {d["path"]: (d["owner"], d["group"], d["mode"]) for d in task["loop"]}
    builder = "{{ homelab_builder_state_dir }}"
    signer = "{{ homelab_builder_signer_dir }}"
    assert got[f"{builder}/staged"] == ("homelab-builder", "homelab-signer", "2770")
    assert got[f"{signer}/gnupg"] == ("homelab-signer", "homelab-signer", "0700")
    assert got[f"{signer}/repo"] == ("homelab-signer", "homelab-signer", "0755")
    assert DEFAULTS["homelab_builder_state_dir"] == "/var/lib/homelab-builder"
    assert DEFAULTS["homelab_builder_signer_dir"] == "/var/lib/homelab-signer"


def test_the_signing_key_is_generated_once_as_the_signer():
    task = task_named(INSTALL, "Generate the archive signing key")
    command = task["ansible.builtin.command"]
    assert "--quick-generate-key" in command["argv"]
    # runuser rather than become_user: the module interpreter is under the 0750 operator home.
    assert command["argv"][:7] == [
        "runuser",
        "-u",
        "homelab-signer",
        "--",
        "gpg",
        "--homedir",
        "{{ homelab_builder_signer_dir }}/gnupg",
    ]
    assert (
        command["creates"] == "{{ homelab_builder_signer_dir }}/gnupg/openpgp-revocs.d"
    )
    assert "become_user" not in task


def test_the_repo_unit_serves_the_signer_tree_on_server_ip_as_a_dynamic_user():
    text = render_setup_text(
        "homelab_builder", "homelab-apt-repo.service.j2", {"server_ip": "192.0.2.7"}
    )
    assert "DynamicUser=yes" in text
    assert "ProtectSystem=strict" in text
    assert (
        "ExecStart=/usr/bin/python3 -m http.server --bind 192.0.2.7 8791 "
        "--directory /var/lib/homelab-signer/repo"
    ) in text


def test_the_ufw_rule_admits_only_the_two_peer_addresses():
    clients = DEFAULTS["homelab_builder_repo_clients"]
    assert clients == [
        "{{ hostvars['daniel-server'].server_ip }}",
        "{{ hostvars['daniel-pi'].server_ip }}",
    ]
    rule = task_named(INSTALL, "Admit the repository's clients")[
        "community.general.ufw"
    ]
    assert rule["port"] == "{{ homelab_builder_repo_port }}"
    assert rule["from_ip"] == "{{ item }}"


def test_the_teardown_removes_every_unit_install_writes():
    units = _units_install_writes()
    assert {
        "homelab-apt-repo.service",
        "homelab-builder.service",
        "homelab-builder.timer",
        "homelab-builder-alert.service",
    } <= units
    removal = task_named(TEARDOWN, "Remove the builder unit files")
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert units <= set(removal["loop"])
    stat = task_named(TEARDOWN, "Check which builder units were installed here")
    assert set(stat["loop"]) == set(removal["loop"])


def test_the_teardown_deletes_the_ufw_rules_install_adds():
    added = task_named(INSTALL, "Admit the repository's clients")
    deleted = task_named(TEARDOWN, "Remove the repository's ufw rules")
    assert deleted["community.general.ufw"]["delete"] is True
    assert deleted["loop"] == added["loop"]
    for key in ("rule", "port", "proto", "from_ip"):
        assert (
            deleted["community.general.ufw"][key] == added["community.general.ufw"][key]
        )


def test_the_teardown_removes_the_builder_state_directory():
    task = task_named(TEARDOWN, "Remove the builder's state directory")
    assert task["ansible.builtin.file"] == {
        "path": "{{ homelab_builder_state_dir }}",
        "state": "absent",
    }


def test_the_teardown_never_names_the_signer_tree_is_clean():
    # fact: ansible/roles/setup/homelab_builder/CLAUDE.md#The teardown keeps the signing key
    assert signer_naming_tasks(INSTALL), "the scan finds nothing in install.yml either"
    assert signer_naming_tasks(TEARDOWN) == []


def test_a_teardown_naming_the_signer_tree_is_flagged():
    red = [
        {
            "name": "by path",
            "ansible.builtin.file": {
                "path": "/var/lib/homelab-signer",
                "state": "absent",
            },
        },
        {
            "name": "by var",
            "ansible.builtin.file": {
                "path": "{{ homelab_builder_signer_dir }}/gnupg",
                "state": "absent",
            },
        },
        {
            "name": "by user",
            "ansible.builtin.user": {"name": "homelab-signer", "state": "absent"},
        },
    ]
    assert signer_naming_tasks(red) == ["by path", "by var", "by user"]
