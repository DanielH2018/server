"""What `narrow` maps a range that RETIRES a role to, and what it still refuses.

The pair `narrow_paths.role_is_gone` owes. A range that deletes a role lists every
path the role owned as changed, so `_role_tags` sees a role with no `containers_list` entry
and no caller — the shape it refuses. Deleting the refusal outright would narrow every such
role, including the one that is merely edited and genuinely applyable by no tag, so the
edited half is the discriminating test.

The fixture is `_narrow_fixtures.build_tree`, shared with `test_deploy_tags_narrow.py`; its
`callers={}` is exactly the no-caller case.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_deletes_a_role.py
"""

import pytest

import narrow_broad

from _narrow_fixtures import Tree, _refs, build_tree

# A role in neither `DECLARED` nor any caller's include list.
RETIRED = "ansible/roles/k8s/valheim-stats/templates/deployment.yaml.j2"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    t.write(RETIRED, "a: b\n")
    t.commit("a role with no containers_list entry and no caller")
    return t


def test_a_range_that_deletes_an_unregistered_role_narrows_to_nothing(tree: Tree):
    """The role directory is gone at the new ref, so no play visits it and no tag applies
    it — the reading a removed `containers_list` entry already gets."""
    tree.remove(RETIRED)
    assert tree.narrow(*_refs(tree)) == set()


def test_a_range_that_edits_that_same_role_still_refuses(tree: Tree):
    """The rejecting half: the role still exists at the new ref, so a tag that applies it
    has to exist, and none does."""
    tree.write(RETIRED, "a: c\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="no containers_list entry"):
        tree.narrow(*_refs(tree))
