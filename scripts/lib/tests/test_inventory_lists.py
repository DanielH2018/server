"""`lib.inventory_lists`: the host list and the Pi's service list.

Run: uv run pytest scripts/lib/tests/test_inventory_lists.py
"""

import re
import sys
from pathlib import Path

import pytest

from lib.inventory_lists import (
    K8S_HOST,
    PI_HOST,
    PI_HOST_VARS,
    host_names,
    pi_entries_in,
)
from docs.fragment_readers import hosts_listing_component
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


def test_role_host_constants_name_the_host_the_inventory_arms():
    """The literal stands in for a package read; this keeps it equal to it."""
    assert hosts_listing_component("pi-docker") == [PI_HOST]


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
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import lib.inventory_lists; "
        "print(sorted(m for m in ('yaml', 'jinja2') if m in sys.modules))"
    )
    out = run([sys.executable, "-c", probe], check=True)
    assert out.stdout.strip() == "[]"
