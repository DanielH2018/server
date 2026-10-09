"""What `narrow` maps a filter-plugin change to, and what it still refuses (#3843).

Each rule in `narrow_filters` is a pair here, as in `test_deploy_tags_narrow.py`: one range it
narrows and one it refuses. The fixture is `_narrow_fixtures.build_tree`, plus one plugin whose
filter `sonarr` alone calls.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_filter_plugin.py
"""

import pytest

import narrow_broad
from lib.repo_paths import REPO

from _narrow_fixtures import Tree, _refs, build_tree

PLUGIN = "ansible/filter_plugins/shout.py"
SONARR = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"


def _plugin(filters: str = '{"shout": shout}', body: str = "x.upper()") -> str:
    return (
        f"def shout(x):\n    return {body}\n\n\n"
        f"class FilterModule:\n    def filters(self):\n        return {filters}\n"
    )


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    t.write(PLUGIN, _plugin())
    t.write(SONARR, "name: {{ 'a' | shout }}\n")
    t.commit("a filter sonarr calls")
    return t


def test_a_filter_only_a_role_template_calls_narrows_to_that_role(tree: Tree):
    tree.write(PLUGIN, _plugin(body="x.upper() + '!'"))
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_filter_the_play_calls_is_flagged(tree: Tree):
    tree.write(
        "ansible/deploy.yml", "- hosts: all\n  vars:\n    p: \"{{ 'a' | shout }}\"\n"
    )
    tree.commit("the play calls it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    with pytest.raises(
        narrow_broad.CannotNarrow, match=r"shout is read by ansible/deploy\.yml"
    ):
        tree.narrow(*_refs(tree))


def test_a_filter_applied_through_map_counts_as_a_call(tree: Tree):
    """`map('shout')` never writes `| shout`, which is why the scan reads the bare name."""
    tree.write(
        "ansible/roles/k8s/radarr/templates/deployment.yaml.j2",
        "names: {{ ['a'] | map('shout') | list }}\n",
    )
    tree.commit("radarr maps it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    assert tree.narrow(*_refs(tree)) == {"sonarr", "radarr"}


def test_a_test_file_beside_a_roles_code_is_not_a_caller(tree: Tree):
    """#3660: `_sort_hits` drops what the deployer's `_is_test_only_path` drops, and a
    `test_*.py` outside a `tests/` directory is one of those."""
    tree.write("ansible/roles/k8s/radarr/files/test_names.py", "assert 'shout'\n")
    tree.commit("radarr's test names it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_filter_named_only_in_comments_reaches_nothing_there(tree: Tree):
    tree.write(SONARR, "{# shout is documented here #}\nname: a\n")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        "# `shout` upper-cases a name\nlan_subnet: 10.0.0.0/24\n",
    )
    tree.commit("only comments name it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    assert tree.narrow(*_refs(tree)) == set()


def test_an_inventory_value_calling_the_filter_is_flagged(tree: Tree):
    """Whatever reads that value is the caller, and the key diff cannot see who that is."""
    tree.write("ansible/inventory/group_vars/all.yml", "loud: \"{{ 'a' | shout }}\"\n")
    tree.commit("an inventory value calls it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    with pytest.raises(narrow_broad.CannotNarrow, match="called by a value in"):
        tree.narrow(*_refs(tree))


def test_a_renamed_filter_still_reaches_its_old_callers(tree: Tree):
    """The names are read at both refs: a caller the range forgot to rename fails to render,
    and the deploy that shows it is the caller's own."""
    tree.write(PLUGIN, _plugin(filters='{"yell": shout}'))
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


@pytest.mark.parametrize(
    "filters",
    ["dict(shout=shout)", "{**OTHER}"],
    ids=["dict-call", "unpacked"],
)
def test_filter_names_no_static_read_can_list_are_flagged(tree: Tree, filters: str):
    tree.write(PLUGIN, _plugin(filters=filters))
    with pytest.raises(narrow_broad.CannotNarrow, match="no literal dict"):
        tree.narrow(*_refs(tree))


def test_a_plugin_another_plugin_imports_is_flagged(tree: Tree):
    tree.write("ansible/filter_plugins/louder.py", "from shout import shout\n")
    tree.commit("a sibling imports it")
    tree.write(PLUGIN, _plugin(body="x.lower()"))
    with pytest.raises(
        narrow_broad.CannotNarrow, match="imported by ansible/filter_plugins/louder"
    ):
        tree.narrow(*_refs(tree))


def test_a_deleted_plugin_is_flagged(tree: Tree):
    tree.remove(PLUGIN)
    with pytest.raises(narrow_broad.CannotNarrow, match="was deleted"):
        tree.narrow(*_refs(tree))


def test_the_real_py_table_plugin_reaches_its_two_callers():
    """Non-vacuity on the real tree, and the issue's own case: `py_table` reached the whole
    play before this rule, and its callers are monitor-bridge and uptime-kuma."""
    ctx = narrow_broad.context_for("HEAD", REPO)
    path = "ansible/filter_plugins/py_table.py"
    assert narrow_broad.broad_path_tags(path, "HEAD", ctx) >= {
        "monitor-bridge",
        "uptime-kuma",
    }
