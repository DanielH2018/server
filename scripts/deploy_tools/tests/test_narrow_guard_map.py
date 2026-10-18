"""What `deploy_tags.py narrow` reaches for a change to the standalone-value guard map (#5571).

The guard task in `manifests` reads `.get(manifests_service, [])` from the map and nothing
else, so a change to the map alone reaches only the callers whose row moved. `land.sh` applies
that rule (`land_shared.guard_map_rows`) and so does the deployer's owed-line check
(`deploy_k8s_owed._guard_row_carries`); these tests hold `broad.narrow` to the same answer.

Every test gives `manifests` all six declared services as callers, as the real fleet does, so
a fan-out to every caller trips the coverage ceiling and reads as a refusal.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_guard_map.py
"""

import pytest

from deploy_tools.narrow_lib import broad as narrow_broad

from _narrow_fixtures import DECLARED, HOST_VARS, Tree, _refs, build_tree

GUARD_MAP = "ansible/roles/k8s/manifests/files/standalone_secret_keys.json"
GUARD_ROWS = '{\n  "radarr": ["radarr_api_key"],\n  "sonarr": ["sonarr_api_key"]\n}\n'


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    t.write("ansible/roles/k8s/manifests/tasks/main.yml", "- debug: {}\n")
    t.write(GUARD_MAP, GUARD_ROWS)
    t.commit("manifests and its guard map")
    return t


def _narrow(tree: Tree, old: str, new: str) -> set[str] | str:
    """`narrow` with every declared service calling `manifests`.

    A refusal comes back as its message, so a test's equality assertion names it.
    """
    try:
        return narrow_broad.narrow(
            old,
            new,
            cwd=tree.root,
            declared=DECLARED,
            callers={"manifests": set(DECLARED)},
        )
    except narrow_broad.CannotNarrow as exc:
        return f"refused: {exc}"


def test_an_added_guard_map_row_narrows_to_that_caller(tree: Tree):
    """The H2.5b shape: a row added for one service reaches that service, not the fleet."""
    tree.write(
        GUARD_MAP, GUARD_ROWS.replace("{\n", '{\n  "bazarr": ["bazarr_api_key"],\n')
    )
    assert _narrow(tree, *_refs(tree)) == {"bazarr"}


def test_a_changed_guard_map_row_narrows_to_that_caller_only(tree: Tree):
    """A key added to sonarr's row leaves radarr's row, and radarr, untouched."""
    tree.write(
        GUARD_MAP,
        GUARD_ROWS.replace('["sonarr_api_key"]', '["sonarr_api_key", "sonarr_push"]'),
    )
    assert _narrow(tree, *_refs(tree)) == {"sonarr"}


def test_a_reserialised_guard_map_narrows_to_nothing(tree: Tree):
    """No row moved, so the guard task reads the same list for every caller."""
    tree.write(
        GUARD_MAP, '{"radarr": ["radarr_api_key"], "sonarr": ["sonarr_api_key"]}\n'
    )
    assert _narrow(tree, *_refs(tree)) == set()


def test_a_removed_row_of_a_retired_role_narrows_to_nothing(tree: Tree):
    """A row whose role has no tag and no caller deploys nothing, rather than refusing."""
    tree.write(
        GUARD_MAP, GUARD_ROWS.replace("{\n", '{\n  "retired": ["retired_api_key"],\n')
    )
    tree.commit("a row for a role about to retire")
    tree.write(GUARD_MAP, GUARD_ROWS)
    assert _narrow(tree, *_refs(tree)) == set()


def test_a_containers_list_edit_beside_a_guard_map_row_narrows_to_both(tree: Tree):
    """The measured refusal: an entry edit plus one map row reaches that entry and that row's
    caller, not most of the fleet."""
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS.replace("image: sonarr:1", "image: sonarr:2"),
    )
    tree.write(
        GUARD_MAP, GUARD_ROWS.replace("{\n", '{\n  "lidarr": ["lidarr_api_key"],\n')
    )
    assert _narrow(tree, *_refs(tree)) == {"sonarr", "lidarr"}


def test_a_manifests_test_beside_the_guard_map_still_narrows_to_the_moved_row(
    tree: Tree,
):
    """Only a NON-test `manifests` path keeps the fan-out; a role test deploys nothing."""
    tree.write("ansible/roles/k8s/manifests/tests/test_guard.py", "def test_x(): ...\n")
    tree.write(
        GUARD_MAP,
        GUARD_ROWS.replace('"radarr_api_key"', '"radarr_api_key", "radarr_push"'),
    )
    assert _narrow(tree, *_refs(tree)) == {"radarr"}


def test_a_manifests_task_edit_beside_a_guard_map_row_keeps_every_caller(tree: Tree):
    """A non-test `manifests` path changes what every caller runs, so the row rule must not
    narrow the range to the one moved row."""
    tree.write("ansible/roles/k8s/manifests/tasks/main.yml", "- debug: {msg: x}\n")
    tree.write(
        GUARD_MAP, GUARD_ROWS.replace("{\n", '{\n  "bazarr": ["bazarr_api_key"],\n')
    )
    assert str(_narrow(tree, *_refs(tree))).startswith("refused:")
