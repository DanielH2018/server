"""`lib.ansible_inventory`: the one reading of the host list and the service lists.

Run: uv run pytest scripts/lib/tests/test_ansible_inventory.py
"""

import re
import sys
from pathlib import Path

import pytest

from lib.ansible_inventory import (
    K8S_HOST,
    PI_HOST,
    PI_HOST_VARS,
    containers_entries_in,
    host_names,
    pi_entries_in,
)
from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import ALL_VARS, HOST_VARS, SCRIPTS


def test_host_names_lists_the_sorted_host_vars_stems_and_skips_a_template(tmp_path):
    for name in ("daniel-server.yml", "daniel-box.yml", "_example.yml"):
        (tmp_path / name).write_text("x: 1\n")
    (tmp_path / "notes.txt").write_text("not a host\n")
    assert host_names(tmp_path) == ("daniel-box", "daniel-server")


def test_the_real_inventory_declares_the_three_hosts():
    """Non-vacuity against ground truth: the derived tuples elsewhere read this list."""
    assert set(host_names()) >= {"daniel-box", "daniel-server", "daniel-pi"}


def hosts_arming(key: str, host_vars: Path = HOST_VARS) -> tuple[str, ...]:
    """The inventory hosts whose host_vars set top-level ``<key>: true``.

    group_vars defaults ``has_docker`` to false, so an explicit true is the only way a host
    takes the role.
    """
    armed = re.compile(rf"^{key}:\s*true\s*(#.*)?$", re.MULTILINE)
    return tuple(
        name
        for name in host_names(host_vars)
        if (host_vars / f"{name}.yml").is_file()
        and armed.search((host_vars / f"{name}.yml").read_text())
    )


def test_role_host_constants_name_the_host_the_inventory_arms():
    """The literals stand in for an inventory read; this keeps them equal to it."""
    assert hosts_arming("has_docker") == (PI_HOST,)


def hosts_declaring_k8s(host_vars: Path = HOST_VARS) -> tuple[str, ...]:
    """The inventory hosts whose host_vars carry a top-level ``containers_list`` key."""
    k8s = re.compile(r"^containers_list:", re.MULTILINE)
    return tuple(
        name
        for name in host_names(host_vars)
        if (host_vars / f"{name}.yml").is_file()
        and k8s.search((host_vars / f"{name}.yml").read_text())
    )


def test_k8s_host_is_the_primary_node_and_no_host_vars_declares_a_containers_list():
    group = yaml_fast.safe_load(ALL_VARS.read_text())
    assert group["k8s_primary_node"] == K8S_HOST
    assert hosts_declaring_k8s() == ()


def test_a_host_vars_file_declaring_a_containers_list_breaks_the_pin(tmp_path):
    (tmp_path / "daniel-box.yml").write_text("containers_list:\n  - name: a\n")
    (tmp_path / "daniel-pi.yml").write_text("containers_list:\n  - name: b\n")
    assert hosts_declaring_k8s(tmp_path) == ("daniel-box", "daniel-pi")


def test_a_second_armed_host_breaks_the_pin(tmp_path):
    """Red proof: a second host taking the role no longer reads as the one constant."""
    (tmp_path / "daniel-box.yml").write_text("has_docker: true\n")
    (tmp_path / "daniel-new.yml").write_text("x: 1\nhas_docker: true  # armed\n")
    (tmp_path / "daniel-off.yml").write_text("has_docker: false\n  has_docker: true\n")
    assert hosts_arming("has_docker", tmp_path) == ("daniel-box", "daniel-new")


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
