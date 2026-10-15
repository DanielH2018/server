"""A change to the standalone-guard map lands only the tags whose row moved (#4932).

`manifests/tasks/main.yml` reads the map's row for its own `manifests_service` and nothing else,
so `land_shared.guard_map_rows` narrows the `manifests` fan-out to the rows that differ over
the PR's range. Every case where it cannot read that answer returns None, which keeps all ~57
callers. The fixture is a throwaway checkout holding the map alone.

Run: uv run pytest scripts/deploy_tools/tests/test_land_shared_guard_map.py
"""

import json

import pytest

from deploy_tools.land_lib.land_shared import GUARD_MAP, guard_map_rows
from lib.git import git

from _narrow_fixtures import Tree, _refs

_ROWS = {"authelia": ["domain"], "traefik": ["domain"]}


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = Tree(tmp_path / "repo")
    t.write(GUARD_MAP, json.dumps(_ROWS, indent=2))
    t.commit("base")
    return t


def _rows(tree: Tree, *files: str):
    old, new = _refs(tree)
    return guard_map_rows(
        list(files),
        f"{old}..{new}",
        lambda *args: git(*args, cwd=tree.root, check=False),
    )


def test_a_diff_adding_one_row_lands_only_that_tag(tree: Tree):
    tree.write(GUARD_MAP, json.dumps(_ROWS | {"home-assistant": ["domain"]}, indent=2))
    assert _rows(tree, GUARD_MAP) == {"home-assistant"}


def test_a_diff_that_breaks_the_json_lands_every_caller(tree: Tree):
    tree.write(GUARD_MAP, '{"authelia": ["domain"],')
    assert _rows(tree, GUARD_MAP) is None


def test_another_manifests_path_beside_the_map_lands_every_caller(tree: Tree):
    tree.write(GUARD_MAP, json.dumps(_ROWS | {"home-assistant": ["domain"]}, indent=2))
    tree.write("ansible/roles/k8s/manifests/tasks/main.yml", "- name: x\n")
    assert _rows(tree, GUARD_MAP, "ansible/roles/k8s/manifests/tasks/main.yml") is None


def test_a_reserialised_map_lands_no_caller(tree: Tree):
    """Parsed-equal rows: the guard reads the same keys for every caller."""
    tree.write(GUARD_MAP, json.dumps(_ROWS))
    assert _rows(tree, GUARD_MAP) == frozenset()


def test_the_landing_and_the_deployer_name_the_same_map():
    """The deployer's `k8s_unapplied` discharge waits on the rows this narrowing deploys
    (#5106), so the two must read one file. `land_shared` keeps its own literal because
    importing the deployer's would pull `host_lib` into `land.py`."""
    from deployer_lib.deploy_k8s_owed import STANDALONE_GUARD_MAP

    assert GUARD_MAP == STANDALONE_GUARD_MAP
