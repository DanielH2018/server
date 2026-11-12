"""`lib.ansible_inventory`: the one reading of `containers_list` and `hosts.ini`.

Run: uv run pytest scripts/lib/tests/test_ansible_inventory.py
"""

import re
import sys
import textwrap
from pathlib import Path

import pytest

from lib.ansible_inventory import (
    K8S_HOST,
    PI_HOST,
    PI_HOST_VARS,
    containers_entries_in,
    host_names,
    inventory_hosts,
    pi_entries_in,
)
from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import ALL_VARS, HOST_VARS, HOSTS_INI, SCRIPTS

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


def test_a_host_in_two_groups_is_one_host_and_a_vars_section_is_not_a_host(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(INI)
    hosts = {h.name: h for h in inventory_hosts(ini)}
    assert hosts["daniel-server"].groups == ("homeservers", "staging")
    assert hosts["daniel-stage"].groups == ("staging",)
    assert "ansible_python_interpreter=/usr/bin/python3" not in hosts


def test_a_quoted_inline_value_reads_as_ansible_reads_it(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(
        "[all]\n"
        """h1 ansible_host="{{server_ip}}" ansible_user='ubuntu'  # a comment's quote\n"""
        """h2 note="a b#c" ansible_user=ubuntu\n"""
    )
    hosts = {h.name: h for h in inventory_hosts(ini)}
    assert hosts["h1"].settings == {
        "ansible_host": "{{server_ip}}",
        "ansible_user": "ubuntu",
    }
    assert hosts["h2"].settings == {"note": "a b#c", "ansible_user": "ubuntu"}


def test_an_unbalanced_quote_fails_naming_the_host(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text('[all]\nh1 ansible_user=ubuntu\nh2 ansible_host="10.9.9.9\n')
    with pytest.raises(ValueError, match="host h2: No closing quotation"):
        inventory_hosts(ini)


@pytest.mark.parametrize(
    "pair, key",
    [
        ("ansible_port=22", "ansible_port"),
        ("""x='"b"'""", "x"),
        ("flag=True", "flag"),
        ('ansible_port="22"', "ansible_port"),
    ],
)
def test_a_value_ansible_evaluates_fails_naming_the_host(tmp_path, pair, key):
    ini = tmp_path / "hosts.ini"
    ini.write_text(f"[all]\nh1 ansible_user=ubuntu\nh2 {pair}\n")
    with pytest.raises(ValueError, match=f"host h2: {key}=.*host_vars"):
        inventory_hosts(ini)


def test_the_real_inventory_declares_the_three_hosts():
    """Non-vacuity against ground truth: the derived tuples elsewhere read this list."""
    assert set(host_names()) >= {"daniel-box", "daniel-server", "daniel-pi"}


def hosts_arming(
    key: str, ini: Path = HOSTS_INI, host_vars: Path = HOST_VARS
) -> tuple[str, ...]:
    """The inventory hosts whose host_vars set top-level ``<key>: true``.

    group_vars defaults ``has_docker`` to false, so an explicit true is the only way a host
    takes the role.
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
    assert hosts_arming("has_docker") == (PI_HOST,)


def hosts_declaring_k8s(
    ini: Path = HOSTS_INI, host_vars: Path = HOST_VARS
) -> tuple[str, ...]:
    """The inventory hosts whose host_vars carry a ``platform: k8s`` entry."""
    k8s = re.compile(r"^\s*platform:\s*k8s\b", re.MULTILINE)
    return tuple(
        name
        for name in host_names(ini)
        if (host_vars / f"{name}.yml").is_file()
        and k8s.search((host_vars / f"{name}.yml").read_text())
    )


def test_k8s_host_is_the_primary_node_and_no_host_vars_declares_a_k8s_entry():
    group = yaml_fast.safe_load(ALL_VARS.read_text())
    assert group["k8s_primary_node"] == K8S_HOST
    assert hosts_declaring_k8s() == ()


def test_a_second_host_declaring_k8s_entries_breaks_the_pin(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text("[all]\ndaniel-box\ndaniel-pi\n")
    (tmp_path / "daniel-box.yml").write_text(
        "containers_list:\n  - name: a\n    platform: k8s\n"
    )
    (tmp_path / "daniel-pi.yml").write_text(
        "containers_list:\n  - name: b\n    platform: k8s\n"
    )
    assert hosts_declaring_k8s(ini, tmp_path) == ("daniel-box", "daniel-pi")


def test_a_second_armed_host_breaks_the_pin(tmp_path):
    """Red proof: a second host taking the role no longer reads as the one constant."""
    ini = tmp_path / "hosts.ini"
    ini.write_text("[all]\ndaniel-box\ndaniel-new\ndaniel-off\n")
    (tmp_path / "daniel-box.yml").write_text("has_docker: true\n")
    (tmp_path / "daniel-new.yml").write_text("x: 1\nhas_docker: true  # armed\n")
    (tmp_path / "daniel-off.yml").write_text("has_docker: false\n  has_docker: true\n")
    assert hosts_arming("has_docker", ini, tmp_path) == ("daniel-box", "daniel-new")


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


def test_pi_entries_in_reads_compose_services_alone():
    data = {
        "containers_list": [{"name": "a"}],
        "compose_services": [{"name": "b"}, {"name": ""}, {"port": 1}, "not-a-mapping"],
    }
    assert [e["name"] for e in pi_entries_in(data)] == ["b"]
    assert pi_entries_in({"compose_services": None}) == []


def test_pi_entries_in_raises_when_compose_services_is_missing():
    with pytest.raises(ValueError, match="compose_services"):
        pi_entries_in({"containers_list": [{"name": "a"}]})


def test_the_live_pi_host_vars_list_the_pis_services():
    """Named members: the helper finds alloy and docker-proxy in the real inventory."""
    names = {
        e["name"] for e in pi_entries_in(yaml_fast.safe_load(PI_HOST_VARS.read_text()))
    }
    assert {"alloy", "docker-proxy"} <= names


def test_importing_it_loads_neither_yaml_nor_jinja2():
    """The reason the module exists: a reader that needs only this must not pay for those."""
    probe = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import lib.ansible_inventory; "
        "print(sorted(m for m in ('yaml', 'jinja2') if m in sys.modules))"
    )
    out = run([sys.executable, "-c", probe], check=True)
    assert out.stdout.strip() == "[]"
