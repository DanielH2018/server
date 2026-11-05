"""Which paths `narrow` reads as deploy-plane, taken from `reach.Reach.deploy_plane`.

`narrow` matched `_BROAD_DEPLOY_PREFIXES` itself, so a test file under `ansible/tasks/`
reached `broad_path_tags` and refused as read by every deploy, though the deployer's own
mapper drops it before any plane (#3660). The play file under the same prefix is the half
that must still refuse.

The fixture is `_narrow_fixtures.build_tree`, shared with `test_deploy_tags_narrow.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_reads_the_deployers_split.py
"""

import pytest

import digest_covers
from deploy_tools.narrow_lib import broad as narrow_broad

from _narrow_fixtures import Tree, _refs, build_tree

_EDIT = "ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2"
PLAY = "- hosts: all\n  vars:\n    p: '{{ play_key }}'\n"


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


def test_a_comment_edit_to_a_play_file_is_clean(tree: Tree):
    tree.write("ansible/tasks/k8s_batch.yml", "- debug: {}\n")
    tree.commit("the task file")
    tree.write("ansible/tasks/k8s_batch.yml", "# Why.\n- debug: {}\n")
    assert tree.narrow(*_refs(tree)) == set()


def test_a_comment_edit_beside_a_service_change_narrows_to_that_service(tree: Tree):
    tree.write("ansible/deploy.yml", PLAY + "# Why.\n")
    tree.write(_EDIT, "a: c\n")
    assert tree.narrow(*_refs(tree)) == {"jellyfin"}


@pytest.mark.parametrize(
    "unread_var",
    ["", "    unread: {}\n"],
    ids=["a-task-value", "a-task-value-beside-an-unread-variable"],
)
def test_a_play_value_python_calls_equal_is_flagged_by_both_readers(
    tree: Tree, unread_var: str
):
    tree.write("ansible/deploy.yml", PLAY + unread_var.format("a") + "  serial: 1\n")
    tree.commit("the play")
    tree.write("ansible/deploy.yml", PLAY + unread_var.format("b") + "  serial: true\n")
    old, new = _refs(tree)
    with pytest.raises(narrow_broad.CannotNarrow, match=r"ansible/deploy\.yml"):
        tree.narrow(old, new)
    why = digest_covers.Classifier(tree.root).reason("sonarr", old, new)
    assert "changes a task" in (why or "")


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        pytest.param(
            "ansible/tasks/k8s_batch.yml",
            "- shell: |\n    a\n",
            "- shell: |\n    # Read by the shell.\n    a\n",
            id="a-comment-shaped-line-in-a-block-scalar",
        ),
        pytest.param(
            "ansible/tasks/k8s_batch.yml",
            "- debug: {}\n",
            "# Why.\n- debug: {\n",
            id="a-file-that-stops-parsing",
        ),
        pytest.param(
            "ansible.cfg",
            "forks = 5\n",
            "# Why.\nforks = 5\n",
            id="a-play-prefix-file-that-is-not-yaml",
        ),
    ],
)
def test_a_play_prefix_edit_that_may_move_content_is_flagged(
    tree: Tree, path: str, before: str, after: str
):
    tree.write(path, before)
    tree.commit("the file")
    tree.write(path, after)
    with pytest.raises(narrow_broad.CannotNarrow, match=path):
        tree.narrow(*_refs(tree))
