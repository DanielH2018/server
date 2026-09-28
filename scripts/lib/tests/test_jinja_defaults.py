"""Tests for lib.jinja_defaults: a generated doc's Jinja resolves only where every host agrees.

Each case builds a throwaway inventory and role under tmp_path, so the verdict never depends
on the real tree's current values.
"""

from pathlib import Path

import pytest

from lib import jinja_defaults
from lib.jinja_defaults import resolve


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """(inventory, role_dir) with one default, one shared var and one host override."""
    inventory = tmp_path / "inventory"
    (inventory / "group_vars").mkdir(parents=True)
    (inventory / "host_vars").mkdir()
    (inventory / "group_vars" / "all.yml").write_text("sys_user: ubuntu\ntick: 5min\n")
    (inventory / "host_vars" / "box.yml").write_text("per_host_minute: '7'\n")
    role = tmp_path / "roles" / "setup" / "demo"
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text(
        "demo_hour: '3'\ntick: 10min\nper_host_minute: '1'\nchained: '{{ demo_hour }}'\n"
    )
    jinja_defaults._inventory_values.cache_clear()
    return inventory, role


def test_a_role_default_resolves(tree):
    inventory, role = tree
    assert resolve("0 {{ demo_hour }} * * *", role, inventory) == "0 3 * * *"


def test_a_filter_expression_resolves(tree):
    inventory, role = tree
    text = "*-*-* {{ '%02d' | format(demo_hour | int) }}:00:00"
    assert resolve(text, role, inventory) == "*-*-* 03:00:00"


def test_group_vars_beats_the_role_default(tree):
    inventory, role = tree
    assert (
        resolve("OnUnitActiveSec={{ tick }}", role, inventory) == "OnUnitActiveSec=5min"
    )


def test_a_host_override_stays_as_written(tree):
    inventory, role = tree
    text = "{{ per_host_minute }} 4 * * *"
    assert resolve(text, role, inventory) == text


def test_an_unknown_variable_stays_as_written(tree):
    inventory, role = tree
    text = "{{ nowhere_defined }} * * * *"
    assert resolve(text, role, inventory) == text


def test_a_value_that_is_itself_a_template_stays_as_written(tree):
    inventory, role = tree
    assert resolve("{{ chained }}", role, inventory) == "{{ chained }}"


def test_an_ansible_only_filter_stays_as_written(tree):
    inventory, role = tree
    text = "{{ demo_hour | to_uuid }}"
    assert resolve(text, role, inventory) == text


def test_the_real_tree_resolves_gitops_deploys_tick_interval():
    """Non-vacuity against the live inventory: the example #2829 was filed about."""
    jinja_defaults._inventory_values.cache_clear()
    role = jinja_defaults.INVENTORY.parent / "roles" / "setup" / "gitops_deploy"
    assert "{{" not in resolve(
        "OnUnitActiveSec={{ gitops_deploy_tick_interval }}", role
    )
