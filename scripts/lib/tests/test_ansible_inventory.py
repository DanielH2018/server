"""`lib.ansible_inventory`: the one reading of `containers_list` and `hosts.ini`.

Run: uv run pytest scripts/lib/tests/test_ansible_inventory.py
"""

import sys
import textwrap

from lib.ansible_inventory import containers_entries_in, host_names, inventory_hosts
from lib.proc_testing import run
from lib.repo_paths import SCRIPTS

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
