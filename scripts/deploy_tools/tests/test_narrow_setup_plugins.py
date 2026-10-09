"""What `narrow_setup.role_tags` narrows a filter-plugin change to, for a setup role calling it.

A plugin reaches a setup role through the vars key whose value calls one of its filters, so
the answer is that key's readers (#3878). Until then the whole role applied: `--tags k3s`
for a `service_tier.py` edit that reaches only `k3s_longhorn_r2_volumes`.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_plugins.py
"""

import pytest

import narrow_setup
from lib.repo_paths import REPO

from _narrow_fixtures import _refs
from _setup_role_fixtures import DEFAULTS, ROLE, Tree, build, narrow

PLUGIN = "ansible/filter_plugins/demo_filters.py"
PLUGIN_TEXT = """\
def demo_double(value):
    return value * {factor}


def demo_unused(value):
    return value


class FilterModule:
    def filters(self):
        return {{"demo_double": demo_double, "demo_unused": demo_unused}}
"""


@pytest.fixture
def calls_plugin(tmp_path, monkeypatch) -> Tree:
    """The demo role, whose `demo_beta_list` calls `demo_double` and feeds `beta.conf.j2`."""
    monkeypatch.setitem(
        narrow_setup.SETUP_ROLES_CALLING_FILTER_PLUGINS, PLUGIN, frozenset({"demo"})
    )
    tree = build(tmp_path)
    tree.write(PLUGIN, PLUGIN_TEXT.format(factor=2))
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS + 'demo_beta_list: "{{ [1] | demo_double }}"\n',
    )
    tree.write(f"{ROLE}/templates/beta.conf.j2", "list = {{ demo_beta_list }}\n")
    tree.commit("the plugin and its caller")
    return tree


def test_a_plugin_change_narrows_to_the_readers_of_the_key_calling_it(calls_plugin):
    calls_plugin.write(PLUGIN, PLUGIN_TEXT.format(factor=3))
    assert narrow(calls_plugin, *_refs(calls_plugin)) == frozenset({"beta"})


def test_a_plugin_change_beside_a_role_change_unions_both(calls_plugin):
    calls_plugin.write(f"{ROLE}/templates/alpha.conf.j2", "mode = 2\n")
    calls_plugin.write(PLUGIN, PLUGIN_TEXT.format(factor=3))
    assert narrow(calls_plugin, *_refs(calls_plugin)) == frozenset({"alpha", "beta"})


def test_a_role_change_beside_an_unchanged_plugin_still_narrows(calls_plugin):
    calls_plugin.write(f"{ROLE}/templates/alpha.conf.j2", "mode = 2\n")
    assert narrow(calls_plugin, *_refs(calls_plugin)) == frozenset({"alpha"})


def test_a_plugin_whose_filters_the_role_never_names_is_flagged(calls_plugin):
    calls_plugin.write(f"{ROLE}/defaults/main.yml", DEFAULTS)
    calls_plugin.commit("drop the caller")
    calls_plugin.write(PLUGIN, PLUGIN_TEXT.format(factor=3))
    with pytest.raises(narrow_setup.CannotNarrow, match="names no filter"):
        narrow(calls_plugin, *_refs(calls_plugin))


def test_a_plugin_with_no_literal_filters_dict_is_flagged(calls_plugin):
    calls_plugin.write(
        PLUGIN, "class FilterModule:\n    def filters(self):\n        return {}\n"
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="no literal dict"):
        narrow(calls_plugin, *_refs(calls_plugin))


def test_an_inventory_value_calling_the_filter_is_flagged(calls_plugin):
    """The role could read that value under a name the key walk never follows."""
    calls_plugin.write(
        "ansible/inventory/group_vars/all.yml", 'shared: "{{ [1] | demo_unused }}"\n'
    )
    calls_plugin.commit("an inventory caller")
    calls_plugin.write(PLUGIN, PLUGIN_TEXT.format(factor=3))
    with pytest.raises(narrow_setup.CannotNarrow, match="called by a value"):
        narrow(calls_plugin, *_refs(calls_plugin))


# ── the real tree: the two callers #3878 names, so the walk cannot go vacuous ────────────


@pytest.mark.parametrize(
    ("role", "plugin", "tags"),
    [
        ("k3s", "service_tier.py", {"longhorn_backup", "longhorn_r2"}),
        ("gitops_deploy", "k8s_autodeploy.py", {"gitops-deploy-service"}),
    ],
)
def test_the_real_plugin_callers_narrow_below_their_role_tag(role, plugin, tags):
    index = narrow_setup.RoleIndex(role, "HEAD", str(REPO))
    path = f"ansible/filter_plugins/{plugin}"
    assert role in narrow_setup.SETUP_ROLES_CALLING_FILTER_PLUGINS[path]
    assert narrow_setup.plugin_tags(path, index, "HEAD", "HEAD", str(REPO)) == tags
