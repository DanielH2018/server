"""`land_tags_cli.py` fans a shared role out to its callers only when the landing would (#4945).

The diff is read from a throwaway checkout holding the one changed file. The caller graph is
the real repo's: `image-builder` has no `containers_list` entry, and `karakeep` is one of the
roles that runs it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_cli.py
"""

import json

import pytest

from deploy_tools.land_lib import land_tags_cli

from _narrow_fixtures import Tree, _refs

TASK_FILE = "ansible/roles/k8s/image-builder/tasks/main.yml"

TASKS = """\
- name: Build the image
  ansible.builtin.command: buildctl build --frontend dockerfile.v0
"""


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = Tree(tmp_path / "repo")
    t.write(TASK_FILE, TASKS)
    t.commit("base")
    return t


def _tags(tree: Tree, capsys) -> list[str]:
    old, new = _refs(tree)
    payload = json.dumps({"files": [{"path": TASK_FILE}], "changedFiles": 1})
    assert (
        land_tags_cli.main(
            ["--json", payload, "--range", f"{old}..{new}"], repo=tree.root
        )
        == 0
    )
    source, _, tags = capsys.readouterr().out.strip().partition(" ")
    assert source == "pr"
    return [t for t in tags.split(",") if t]


def test_a_comment_only_shared_role_edit_reaches_no_caller(tree: Tree, capsys):
    """CLEAN half: the landing deploys nothing for this edit, so the CLI names nothing."""
    tree.write(TASK_FILE, "# DECIDED: a comment and nothing else.\n" + TASKS)
    assert _tags(tree, capsys) == []


def test_a_real_shared_role_edit_reaches_its_callers(tree: Tree, capsys):
    """FLAGGED half: a task edit can move rendered bytes, so every caller deploys."""
    tree.write(TASK_FILE, TASKS.replace("dockerfile.v0", "gateway.v0"))
    assert "karakeep" in _tags(tree, capsys)
