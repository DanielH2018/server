"""`lib.ansible_inventory`: the one reading of `containers_list` and `hosts.ini`.

Run: uv run pytest scripts/lib/tests/test_ansible_inventory.py
"""

import re
import sys
import textwrap
from pathlib import Path

from lib.ansible_inventory import (
    GITOPS_HOST,
    PI_HOST,
    containers_entries_in,
    host_names,
    inventory_hosts,
)
from lib.proc_testing import run
from lib.repo_paths import HOST_VARS, HOSTS_INI, SCRIPTS

INI = textwrap.dedent("""\
    # a leading comment
    ; an ini-style comment
    [homeservers]
    daniel-server  ansible_connection=local  # an inline comment
    daniel-pi   ansible_host=10.0.0.139 ansible_user=ubuntu
    [staging]
    daniel-stage ansible_connection=local
    daniel-server
    [homeservers:vars]
    ansible_python_interpreter=/usr/bin/python3
    """)


def test_host_lines_carry_their_settings_and_drop_an_inline_comment(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(INI)
    hosts = {h.name: h for h in inventory_hosts(ini)}
    assert host_names(ini) == ("daniel-server", "daniel-pi", "daniel-stage")
    assert hosts["daniel-server"].settings == {"ansible_connection": "local"}
    assert hosts["daniel-pi"].settings["ansible_host"] == "10.0.0.139"


def test_connection_defaults_to_ssh_as_ansible_does(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(INI)
    hosts = {h.name: h for h in inventory_hosts(ini)}
    assert hosts["daniel-server"].connection == "local"
    assert hosts["daniel-pi"].connection == "ssh"


def test_a_host_in_two_groups_is_one_host_and_a_vars_section_is_not_a_host(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(INI)
    hosts = {h.name: h for h in inventory_hosts(ini)}
    assert hosts["daniel-server"].groups == ("homeservers", "staging")
    assert hosts["daniel-stage"].groups == ("staging",)
    assert "ansible_python_interpreter=/usr/bin/python3" not in hosts


def test_the_real_inventory_declares_the_three_hosts():
    """Non-vacuity against ground truth: the derived tuples elsewhere read this list."""
    assert set(host_names()) >= {"daniel-box", "daniel-server", "daniel-pi"}


def hosts_arming(
    key: str, ini: Path = HOSTS_INI, host_vars: Path = HOST_VARS
) -> tuple[str, ...]:
    """The inventory hosts whose host_vars set top-level ``<key>: true``.

    group_vars defaults ``has_gitops`` and ``has_docker`` to false, so an explicit true is the
    only way a host takes either role.
    """
    armed = re.compile(rf"^{key}:\s*true\s*(#.*)?$", re.MULTILINE)
    return tuple(
        name
        for name in host_names(ini)
        if (host_vars / f"{name}.yml").is_file()
        and armed.search((host_vars / f"{name}.yml").read_text())
    )


def test_role_host_constants_name_the_host_the_inventory_arms():
    """The literals stand in for an inventory read; this keeps them equal to it."""
    assert hosts_arming("has_gitops") == (GITOPS_HOST,)
    assert hosts_arming("has_docker") == (PI_HOST,)


def test_a_second_armed_host_breaks_the_pin(tmp_path):
    """Red proof: a second host taking the role no longer reads as the one constant."""
    ini = tmp_path / "hosts.ini"
    ini.write_text("[all]\ndaniel-box\ndaniel-new\ndaniel-off\n")
    (tmp_path / "daniel-box.yml").write_text("has_gitops: true\n")
    (tmp_path / "daniel-new.yml").write_text("x: 1\nhas_gitops: true  # armed\n")
    (tmp_path / "daniel-off.yml").write_text("has_gitops: false\n  has_gitops: true\n")
    assert hosts_arming("has_gitops", ini, tmp_path) == ("daniel-box", "daniel-new")


def test_containers_entries_keep_named_mappings_only():
    data = {
        "containers_list": [
            {"name": "a"},
            {"name": ""},
            {"platform": "k8s"},
            "not-a-mapping",
            {"name": "b", "platform": "k8s"},
        ]
    }
    assert [e["name"] for e in containers_entries_in(data)] == ["a", "b"]
    assert containers_entries_in(None) == []
    assert containers_entries_in({"containers_list": None}) == []


def test_importing_it_loads_neither_yaml_nor_jinja2():
    """The reason the module exists: a reader that needs only this must not pay for those."""
    probe = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import lib.ansible_inventory; "
        "print(sorted(m for m in ('yaml', 'jinja2') if m in sys.modules))"
    )
    out = run([sys.executable, "-c", probe], check=True)
    assert out.stdout.strip() == "[]"
