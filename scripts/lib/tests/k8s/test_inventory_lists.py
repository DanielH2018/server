"""`lib.inventory_lists`: the host list and the Pi's service list.

Run: uv run pytest scripts/lib/tests/k8s/test_inventory_lists.py
"""

import sys

import pytest

from lib.inventory_lists import (
    K8S_HOST,
    PI_HOST,
    host_names,
    host_vars_file,
    pi_entries_in,
)
from docs.fragment_readers import hosts_listing_component
from lib.proc_testing import run
from lib.estate import REPO_INVENTORY, Estate, Inventory, host_vars, shared_vars
from lib.repo_paths import SCRIPTS


def test_host_names_lists_the_sorted_host_vars_stems_and_skips_a_template(tmp_path):
    for name in ("daniel-server", "daniel-box", "_example"):
        host_vars_file(name, tmp_path).parent.mkdir()
        host_vars_file(name, tmp_path).write_text("x: 1\n")
    (tmp_path / "notes.txt").write_text("not a host\n")
    (tmp_path / "no-vars").mkdir()
    assert host_names(tmp_path) == ("daniel-box", "daniel-server")


def test_the_real_inventory_declares_the_three_hosts():
    """Non-vacuity against ground truth: the derived tuples elsewhere read this list."""
    assert set(host_names()) >= {"daniel-box", "daniel-server", "daniel-pi"}


def test_role_host_constants_name_the_host_the_inventory_arms():
    """The literal stands in for a package read; this keeps it equal to it."""
    assert hosts_listing_component("pi-docker") == [PI_HOST]


def hosts_declaring_k8s(inventory: Inventory = REPO_INVENTORY) -> tuple[str, ...]:
    """The inventory hosts whose host_vars carry a top-level ``containers_list`` key."""
    estate = Estate(inventory)
    return tuple(
        name for name in estate.hosts() if "containers_list" in estate.own_vars(name)
    )


def test_k8s_host_is_the_primary_node_and_no_host_vars_declares_a_containers_list():
    group = shared_vars()
    assert group["k8s_primary_node"] == K8S_HOST
    assert hosts_declaring_k8s() == ()


def test_a_host_vars_file_declaring_a_containers_list_breaks_the_pin(tmp_path):
    for name, item in (("daniel-box", "a"), ("daniel-pi", "b")):
        host_vars_file(name, tmp_path).parent.mkdir()
        host_vars_file(name, tmp_path).write_text(
            f"containers_list:\n  - name: {item}\n"
        )
    assert hosts_declaring_k8s(Inventory(host_vars=tmp_path)) == (
        "daniel-box",
        "daniel-pi",
    )


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
    names = {e["name"] for e in pi_entries_in(host_vars(PI_HOST))}
    assert {"alloy", "docker-proxy"} <= names


def test_importing_it_loads_neither_yaml_nor_jinja2():
    """The reason the module exists: a reader that needs only this must not pay for those."""
    probe = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import lib.inventory_lists; "
        "print(sorted(m for m in ('yaml', 'jinja2') if m in sys.modules))"
    )
    out = run([sys.executable, "-c", probe], check=True)
    assert out.stdout.strip() == "[]"
