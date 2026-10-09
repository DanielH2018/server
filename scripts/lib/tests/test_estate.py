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
    (tmp_path / "hosts.ini").write_text("[homeservers]\nbox\npi\nbare\n")
    (tmp_path / "all.yml").write_text(
        yaml.safe_dump({"port": 1, "has_docker": False, "alias": "{{ sys_user }}"})
    )
    (host_vars / "box.yml").write_text(
        yaml.safe_dump(
            {
                "port": 2,
                "containers_list": [
                    {"name": "web", "platform": "k8s"},
                    {"name": "old", "platform": "docker"},
                    {"platform": "k8s"},
                ],
            }
        )
    )
    (host_vars / "pi.yml").write_text(
        yaml.safe_dump(
            {
                "has_docker": True,
                "containers_list": [{"name": "wg"}, {"name": "x", "platform": "k8s"}],
            }
        )
    )
    return Estate(
        Inventory(
            all_vars=tmp_path / "all.yml",
            host_vars=host_vars,
            hosts_ini=tmp_path / "hosts.ini",
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
    assert estate.hosts_in("homeservers") == ["box", "pi", "bare"]
    assert estate.host_vars_hosts() == ["box", "pi"]


def test_entries_split_by_platform_and_drop_the_unnamed(estate):
    assert list(estate.k8s_entries()) == ["web"]
    assert [e["name"] for e in estate.pi_entries("pi")] == ["wg"]
