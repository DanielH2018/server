"""The setup census derives what the setup roles still own, and fails when it cannot see one.

`scripts/deploy_tools/setup_lib/setup_census.py` walks every setup role's tasks, crons and
timers and resolves each item against the host packages (H7.2f). Three properties make the list
trustworthy:

1. It finds the named members of each kind on the real tree. A census that stops matching
   fails by name here instead of agreeing with an empty set.
2. Each module that writes something is an item, and only a control or read-only module is not.
   A module that leaks into the control list drops out of the report silently, so the red case
   passes such a list and shows the census no longer reports apt and get_url.
3. A `delegate_to:` task counts against the delegated host, and an emptied role's stub does not
   count at all.

Run: uv run pytest ansible/tests/setup/test_setup_census.py
"""

from pathlib import Path

import pytest
import yaml
from deploy_tools.setup_lib.setup_census import (
    ALLOWLIST,
    HostSide,
    items_left,
    resolved,
    strict_exit,
)
from deploy_tools.setup_lib.setup_census_items import CONTROL_MODULES, Item

HOSTS = ("daniel-box", "daniel-pi")


def _fixture_role(tmp_path: Path, tasks: list[dict], name: str = "demo") -> Path:
    """A setup tree holding one role whose `tasks/main.yml` is `tasks`; returns the tree."""
    setup = tmp_path / "setup"
    (setup / name / "tasks").mkdir(parents=True, exist_ok=True)
    (setup / name / "tasks" / "main.yml").write_text(yaml.safe_dump(tasks))
    return setup


def _left(tmp_path: Path, tasks: list[dict], **kwargs) -> dict[str, list[Item]]:
    """The census of a one-role fixture that reaches daniel-box only."""
    setup = _fixture_role(tmp_path, tasks)
    return items_left(
        hosts=HOSTS,
        setup=setup,
        hosts_root=tmp_path / "hosts",
        reach_of=lambda role: frozenset({"daniel-box"}),
        ctx_of=lambda role_dir, host: {},
        repo=tmp_path,
        **kwargs,
    )


def _names(left: dict[str, list[Item]], host: str) -> set[str]:
    return {item.name for item in left[host]}


# --- the real tree ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_tree() -> dict[str, list[Item]]:
    """The census of the few roles the named members live in, on every host."""
    roles = frozenset(
        {
            "initial_setup",
            "claude_code",
            "k3s",
            "sops_setup",
            "nut_host",
            "k8s_host_files",
        }
    )
    return items_left(roles=roles)


NAMED_MEMBERS = [
    ("cron", "Weekly apt autoremove"),
    ("template", "Install the lander unit"),
    ("template", "Pin the node's LAN address in netplan"),
    ("get_url", "Download and install SOPS binary"),
    ("systemd_service", "Enable and start the host upsmon service"),
    ("sysctl", "Apply kernel security hardening"),
    ("ufw", "Enable UFW firewall"),
]


@pytest.mark.parametrize(("kind", "name"), NAMED_MEMBERS)
def test_the_census_finds_each_named_member(real_tree, kind, name):
    found = [
        item
        for items in real_tree.values()
        for item in items
        if item.kind == kind and item.name == name
    ]
    assert found, f"no {kind} item named {name!r} in the census"


def test_a_shipped_file_and_a_target_are_both_searchable(real_tree):
    items = [i for items in real_tree.values() for i in items]
    assert any(i.mentions("claude-land@.service.j2") for i in items)
    assert any(i.mentions("k3s/tasks/static-address.yml") for i in items)
    assert any(i.mentions("/usr/local/bin/sops") for i in items)
    assert any(i.mentions("net.core.bpf_jit_harden") for i in items)


def test_the_pi_peer_backup_wrapper_counts_against_daniel_pi(real_tree):
    wrapper = "/usr/local/bin/pi-peer-backup-shell"
    on_pi = [i for i in real_tree["daniel-pi"] if i.target == wrapper]
    assert on_pi
    assert all(i.role == "k8s_host_files" for i in on_pi)


# --- what an item is --------------------------------------------------------------------------


def test_an_unassigned_cron_is_flagged_and_strict_mode_fails(tmp_path):
    left = _left(
        tmp_path,
        [
            {
                "name": "Weekly demo job",
                "ansible.builtin.cron": {"name": "demo", "job": "/bin/true"},
            }
        ],
    )
    assert _names(left, "daniel-box") == {"demo"}
    assert strict_exit(left) == 1


def test_a_cron_task_and_its_at_a_glance_entry_are_one_item(tmp_path):
    left = _left(
        tmp_path,
        [{"name": "Schedule", "ansible.builtin.cron": {"name": "demo", "job": "x"}}],
    )
    assert [i.kind for i in left["daniel-box"]] == ["cron"]


def test_apt_and_get_url_are_reported_and_a_read_is_not(tmp_path):
    left = _left(
        tmp_path,
        [
            {"name": "Install jq", "ansible.builtin.apt": {"name": "jq"}},
            {
                "name": "Fetch tool",
                "ansible.builtin.get_url": {"dest": "/x", "url": "u"},
            },
            {
                "name": "Read the version",
                "ansible.builtin.command": "tool --version",
                "changed_when": False,
            },
        ],
    )
    assert _names(left, "daniel-box") == {"Install jq", "Fetch tool"}


def test_a_command_with_creates_is_an_item_even_when_unchanged(tmp_path):
    left = _left(
        tmp_path,
        [
            {
                "name": "Build once",
                "ansible.builtin.command": {"cmd": "make", "creates": "/opt/x"},
                "changed_when": False,
            }
        ],
    )
    assert _names(left, "daniel-box") == {"Build once"}


def test_a_control_module_that_swallows_apt_and_get_url_hides_them(tmp_path):
    tasks = [
        {"name": "Install jq", "ansible.builtin.apt": {"name": "jq"}},
        {"name": "Fetch tool", "ansible.builtin.get_url": {"dest": "/x", "url": "u"}},
    ]
    leaky = CONTROL_MODULES | {"apt", "get_url"}
    assert _names(_left(tmp_path, tasks, control=leaky), "daniel-box") == set()
    assert _names(_left(tmp_path, tasks), "daniel-box") == {"Install jq", "Fetch tool"}


def test_an_import_of_a_common_task_file_is_an_item(tmp_path):
    left = _left(
        tmp_path,
        [{"name": "Alert", "import_tasks": "../../common/tasks/alert_unit.yml"}],
    )
    assert [(i.kind, i.name) for i in left["daniel-box"]] == [
        ("import", "alert_unit.yml")
    ]


def test_an_import_of_a_role_local_file_is_not_an_item(tmp_path):
    assert not any(_left(tmp_path, [{"import_tasks": "other.yml"}]).values())


# --- which host an item counts against --------------------------------------------------------


def test_a_delegated_task_counts_against_the_delegated_host(tmp_path):
    left = _left(
        tmp_path,
        [
            {
                "name": "Authorize the key",
                "ansible.posix.authorized_key": {"key": "k", "user": "u"},
                "delegate_to": "daniel-pi",
            }
        ],
    )
    assert _names(left, "daniel-pi") == {"Authorize the key"}
    assert _names(left, "daniel-box") == set()


def test_a_block_delegate_applies_to_the_tasks_inside_it(tmp_path):
    left = _left(
        tmp_path,
        [
            {
                "delegate_to": "daniel-pi",
                "block": [{"name": "Write it", "ansible.builtin.copy": {"dest": "/d"}}],
            }
        ],
    )
    assert _names(left, "daniel-pi") == {"Write it"}
    assert _names(left, "daniel-box") == set()


def test_an_emptied_role_stub_is_not_flagged_in_strict_mode(tmp_path):
    left = _left(
        tmp_path,
        [{"name": "End the role here", "meta": "end_role"}],
    )
    assert strict_exit(left) == 0


# --- resolution -------------------------------------------------------------------------------


def _side(**kwargs) -> HostSide:
    side = HostSide()
    for key, value in kwargs.items():
        getattr(side, key).update(value)
    return side


def test_a_path_a_package_installs_resolves_and_another_path_does_not():
    item = Item("daniel-box", "r", "template", "Unit", target="/etc/demo.conf")
    assert resolved(item, _side(paths={"/etc/demo.conf"}))
    assert not resolved(item, _side(paths={"/etc/other.conf"}))


def test_a_legacy_cron_resolves_the_cron_with_that_name():
    item = Item("daniel-box", "r", "cron", "demo")
    assert resolved(item, _side(crons={"demo"}))
    assert not resolved(item, _side(crons={"other"}))


def test_an_apt_item_resolves_only_when_every_package_is_a_depends_entry():
    item = Item("daniel-box", "r", "apt", "Install", target="['curl', 'jq']")
    assert resolved(item, _side(depends={"curl", "jq", "git"}))
    assert not resolved(item, _side(depends={"curl"}))


def test_an_allowlisted_name_resolves_and_the_list_names_its_reason():
    item = Item("daniel-box", "r", "file", "Clear ansible log file")
    assert resolved(item, HostSide())
    assert not resolved(item, HostSide(), allowlist={})
    assert all(ALLOWLIST.values())


# --- the per-host guard -----------------------------------------------------------------------

GUARDED = {
    "name": "Write the demo config",
    "ansible.builtin.copy": {"dest": "/etc/demo.conf", "content": "x"},
    "when": "inventory_hostname not in demo_packaged_hosts['config']",
}


def test_a_guard_naming_the_host_does_not_hide_an_item_its_package_lacks(tmp_path):
    left = _left(tmp_path, [GUARDED])
    [item] = left["daniel-box"]
    assert item.guarded
    assert "per-host guard" in item.line()


def test_a_guarded_item_resolves_once_the_hosts_package_carries_it():
    item = Item("daniel-box", "demo", "copy", "Write", "/etc/demo.conf", guarded=True)
    assert resolved(item, _side(paths={"/etc/demo.conf"}))
    assert not resolved(item, HostSide())


def test_an_unguarded_item_carries_no_guard_marker(tmp_path):
    plain = {k: v for k, v in GUARDED.items() if k != "when"}
    [item] = _left(tmp_path, [plain])["daniel-box"]
    assert not item.guarded
    assert "per-host guard" not in item.line()
