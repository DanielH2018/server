"""Which paths `narrow` reads as deploy-plane, taken from `reach.Reach.deploy_plane`.

`narrow` matched `_BROAD_DEPLOY_PREFIXES` itself, so a test file under `ansible/tasks/`
reached `broad_path_tags` and refused as read by every deploy, though the deployer's own
mapper drops it before any plane (#3660). The play file under the same prefix is the half
that must still refuse.

The fixture is `_narrow_fixtures.build_tree`, shared with `test_deploy_tags_narrow.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_reads_the_deployers_split.py
"""

import pytest

import narrow_broad

from _narrow_fixtures import Tree, _refs, build_tree

_EDIT = "ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2"


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build_tree(tmp_path)


def test_a_test_file_under_a_play_prefix_narrows_like_the_tick_reads_it(tree: Tree):
    tree.write("ansible/tasks/tests/test_k8s_batch.py", "def test_x(): pass\n")
    tree.write(_EDIT, "a: c\n")
    assert tree.narrow(*_refs(tree)) == {"jellyfin"}


def test_a_play_file_under_the_same_prefix_still_refuses(tree: Tree):
    tree.write("ansible/tasks/k8s_batch.yml", "- debug: {}\n")
    tree.write(_EDIT, "a: c\n")
    with pytest.raises(
        narrow_broad.CannotNarrow, match=r"ansible/tasks/k8s_batch\.yml"
    ):
        tree.narrow(*_refs(tree))
