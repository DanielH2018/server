"""The inventory loader the docs generators read through.

Each test lays out a throwaway inventory under `tmp_path`, so a precedence collision is
checked against a tree that carries one on purpose.

Run: uv run pytest scripts/lib/tests/test_estate.py
"""

from pathlib import Path

import pytest
import yaml

from lib.estate import Estate, Inventory


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (tmp_path / "services.yml").write_text(
        yaml.safe_dump(
            {"containers_list": [{"name": "web"}, {"name": "db"}, {"port": 1}]}
        )
    )
    (tmp_path / "all.yml").write_text(
        yaml.safe_dump({"port": 1, "has_docker": False, "alias": "{{ sys_user }}"})
    )
    (host_vars / "box.yml").write_text(
        yaml.safe_dump(
            {
                "port": 2,
            }
        )
    )
    (host_vars / "pi.yml").write_text(
        yaml.safe_dump(
            {
                "has_docker": True,
                "compose_services": [{"name": "wg"}],
            }
        )
    )
    return Estate(
        Inventory(
            all_vars=tmp_path / "all.yml",
            host_vars=host_vars,
            services=tmp_path / "services.yml",
            plane_hosts={"k8s": "box"},
        )
    )


def test_host_beats_group_beats_role_default(estate):
    merged = estate.vars("box", defaults={"port": 0, "own": "default"})
    assert (merged["port"], merged["own"]) == (2, "default")
    assert estate.vars("pi", defaults={"port": 0})["port"] == 1


def test_values_arrive_raw_rather_than_resolved(estate):
    # A page prints what the file says; render_context is the reader that expands Jinja.
    assert estate.vars("box")["alias"] == "{{ sys_user }}"


def test_source_names_the_layer_a_value_comes_from(estate):
    assert estate.source("pi", "has_docker") == "host"
    assert estate.source("box", "has_docker") == "group"
    assert estate.source("box", "never_declared") is None


def test_a_host_without_a_host_vars_file_reads_the_group_layer(estate):
    assert estate.own_vars("bare") == {}
    assert estate.vars("bare")["port"] == 1
    assert estate.hosts() == ["box", "pi"]


def test_k8s_entries_are_the_named_services_keyed_by_name(estate):
    assert list(estate.k8s_entries()) == ["web", "db"]


def test_role_vars_lets_the_inventory_override_a_role_default(estate, tmp_path):
    # A fragment printing the raw default would be wrong once all.yml or the plane host's
    # host_vars sets the same key.
    for role in (tmp_path / "k8s" / "roles" / "r", tmp_path / "roles" / "setup" / "r"):
        (role / "defaults").mkdir(parents=True)
        (role / "defaults" / "main.yml").write_text("port: 0\nown: default\n")
    k8s = estate.role_vars(tmp_path / "k8s" / "roles" / "r")
    setup = estate.role_vars(tmp_path / "roles" / "setup" / "r")
    assert (k8s["port"], k8s["own"]) == (2, "default")
    assert setup["port"] == 1
