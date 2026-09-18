"""What `deploy_tags.py narrow` maps a deploy-plane range to, and what it refuses.

Every rule in `narrow_broad.py` is a PAIR here: one range it narrows to a tag list (or to
nothing, on purpose) and one it refuses with `DEPLOY_BROAD`. A narrowing that fired on
everything and one that fired on nothing read identically from the passing side alone, and
this module is the only place that tells them apart.

The fixture is `_narrow_fixtures.build_tree`: a throwaway git repo with the shape the rules
read, shared with `test_deploy_tags_narrow_cmd.py`.

`test_the_real_tree_still_has_macro_importers` is the non-vacuity half: the importer scan
finds its subject by pattern, so a changed Jinja spelling would make it return an empty set
that reads as "this macro reaches nothing" rather than as a broken scan.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags_narrow.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import narrow_broad
from lib.repo_paths import REPO

from _narrow_fixtures import (
    DECLARED,
    GROUP_VARS,
    HOST_VARS,
    Tree,
    _refs,
    build_tree,
)


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build_tree(tmp_path)


# ── containers_list: an entry maps to its own tag ───────────────────────────────────────


def test_a_changed_containers_list_entry_narrows_to_that_service(tree: Tree):
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS.replace("image: sonarr:1", "image: sonarr:2"),
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_an_added_containers_list_entry_narrows_to_its_tag(tree: Tree):
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS + "  - name: jellyfin-new\n    platform: k8s\n",
    )
    assert tree.narrow(*_refs(tree)) == {"jellyfin-new"}


def test_a_removed_containers_list_entry_is_flagged(tree: Tree):
    """A removal has no tag that undoes it — only the whole play reconciles the host."""
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS.replace("  - name: bazarr\n    platform: k8s\n", ""),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="removed"):
        tree.narrow(*_refs(tree))


# ── a plain inventory key maps to the roles that read it ────────────────────────────────


def test_a_group_vars_key_two_roles_read_narrows_to_both(tree: Tree):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.1.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "radarr"}


def test_a_group_vars_key_the_play_reads_is_flagged(tree: Tree):
    """`ansible/deploy.yml` reads it, so no tag scopes the change."""
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("read-by-the-play", "read-by-the-play-still"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="ansible/deploy.yml"):
        tree.narrow(*_refs(tree))


def test_a_group_vars_key_nothing_reads_narrows_to_nothing(tree: Tree):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("nobody-reads-this", "still-nobody"),
    )
    assert tree.narrow(*_refs(tree)) == set()


def test_a_key_most_of_the_fleet_reads_is_flagged(tree: Tree):
    """Four of six declared services read it: a tag list that wide is not a narrowing."""
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("everywhere", "everywhere-else"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="most of the fleet"):
        tree.narrow(*_refs(tree))


def test_a_comment_only_inventory_change_narrows_to_nothing(tree: Tree):
    """The rejecting half of every key rule: no content moved, so nothing is stale.

    There is no comment-only rule to reach it — the keys are compared as parsed VALUES, and
    a comment is not one. `comment_only_broad_changes` answers the same question for the
    setup plane by comparing lines, which is what a plane with no key structure needs.
    """
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("# The LAN, read by two roles.", "# The LAN. Two roles.\n"),
    )
    assert tree.narrow(*_refs(tree)) == set()


# ── shared macros map to their importers ────────────────────────────────────────────────


def test_a_macro_change_narrows_to_its_importers(tree: Tree):
    tree.write(
        "ansible/templates/container-resources.yml.j2",
        "{% macro resources(cpu) %}\n",
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_macro_nothing_imports_narrows_to_nothing(tree: Tree):
    tree.write("ansible/templates/orphan.yml.j2", "{% macro orphan(x) %}\n")
    assert tree.narrow(*_refs(tree)) == set()


def test_a_macro_named_by_a_filter_plugin_is_clean(tree: Tree):
    """A `.py` under `filter_plugins/` cannot render a macro; `toposort.py` names
    `ingressroute.yml.j2` as the marker it greps role templates for (#2001)."""
    tree.write(
        "ansible/filter_plugins/toposort.py",
        'MARKERS = ("traefik.io", "container-resources.yml.j2")\n',
    )
    tree.commit("a filter plugin names the macro")
    tree.write(
        "ansible/templates/container-resources.yml.j2", "{% macro resources(c) %}\n"
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr"}


def test_a_macro_named_by_the_play_itself_is_flagged(tree: Tree):
    """The pair: the exemption is the filter-plugin path class, not play-level files."""
    tree.write(
        "ansible/deploy.yml",
        "- hosts: all\n  vars:\n    p: '{{ play_key }}'\n    m: container-resources.yml.j2\n",
    )
    tree.commit("the play names the macro")
    tree.write(
        "ansible/templates/container-resources.yml.j2", "{% macro resources(c) %}\n"
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="ansible/deploy.yml"):
        tree.narrow(*_refs(tree))


def test_a_variable_a_filter_plugin_reads_is_still_flagged(tree: Tree):
    """The exemption is for the macro scan only: a filter plugin CAN read a variable."""
    tree.write("ansible/filter_plugins/toposort.py", "KEY = 'unused_key'\n")
    tree.commit("a filter plugin reads the key")
    tree.write(
        "ansible/inventory/group_vars/all.yml", GROUP_VARS.replace("nobody", "x")
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="filter_plugins/toposort.py"):
        tree.narrow(*_refs(tree))


def test_a_macro_reaching_an_uncallable_role_names_the_macro(tree: Tree):
    """FLAGGED half: the refusal comes from `_role_tags`, which knows only the role.

    Paired with `test_a_macro_change_narrows_to_its_importers` above. Without the macro name
    and the derivation line, the tick's journal says a role refused and nothing says which
    changed template reached it.
    """
    tree.write(
        "ansible/roles/k8s/shared/templates/x.yaml.j2",
        "{% from 'orphan.yml.j2' import orphan %}\n",
    )
    tree.commit("a shared role nothing calls imports the macro")
    tree.write("ansible/templates/orphan.yml.j2", "{% macro orphan(x) %}\n")
    old, new = _refs(tree)
    lines: list[str] = []
    with pytest.raises(narrow_broad.CannotNarrow, match="orphan.yml.j2") as exc:
        narrow_broad.narrow(
            old, new, cwd=tree.root, declared=DECLARED, callers={}, explain=lines.append
        )
    assert "no caller" in str(exc.value)
    assert "narrow: orphan.yml.j2 -> roles shared" in lines[-1]


def test_a_key_read_only_through_a_play_level_macro_names_the_key(tree: Tree):
    """FLAGGED half: the refusal comes from inside `template_importers`, which knows the
    macro and not the key that reached it."""
    tree.write("ansible/templates/played.yml.j2", "p: {{ played_key }}\n")
    tree.write(
        "ansible/deploy.yml",
        "- hosts: all\n  vars:\n    p: \"{% include 'played.yml.j2' %}\"\n",
    )
    tree.write("ansible/inventory/group_vars/all.yml", GROUP_VARS + "played_key: 1\n")
    tree.commit("a key only a play-level macro reads")
    tree.write("ansible/inventory/group_vars/all.yml", GROUP_VARS + "played_key: 2\n")
    old, new = _refs(tree)
    lines: list[str] = []
    with pytest.raises(narrow_broad.CannotNarrow, match="played_key") as exc:
        narrow_broad.narrow(
            old, new, cwd=tree.root, declared=DECLARED, callers={}, explain=lines.append
        )
    assert "every deploy runs" in str(exc.value)
    assert "narrow: played_key -> macro played.yml.j2" in lines[-1]


def test_the_real_tree_still_has_macro_importers():
    """Non-vacuity: the importer scan must still find the macro 82 role templates import.

    It finds its subject by pattern, so a changed Jinja spelling returns an empty set that
    reads as "this macro reaches nothing" — a narrowing to nothing where a fleet-wide refusal
    belongs.
    """
    found = narrow_broad.template_importers(
        "container-resources.yml.j2", "HEAD", REPO, set()
    )
    assert len(found) >= 20, found


# ── the paths no rule can scope ─────────────────────────────────────────────────────────


def test_a_play_level_file_is_flagged(tree: Tree):
    tree.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: 'x'\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="ansible/deploy.yml"):
        tree.narrow(*_refs(tree))


def test_hosts_ini_is_flagged(tree: Tree):
    tree.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\ndaniel-pi\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="hosts.ini"):
        tree.narrow(*_refs(tree))


def test_a_deleted_broad_path_is_flagged(tree: Tree):
    """Nothing can be read at the new ref, so nothing can be derived from it."""
    tree.remove("ansible/templates/orphan.yml.j2")
    with pytest.raises(narrow_broad.CannotNarrow, match="deleted"):
        tree.narrow(*_refs(tree))


# ── the non-broad half is the mapper `changed` already uses ─────────────────────────────


def test_a_non_broad_path_maps_the_way_changed_maps_it(tree: Tree):
    """A k8s role beside the inventory change contributes the tag `changed` would derive."""
    tree.write("ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2", "a: c\n")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.2.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"jellyfin", "sonarr", "radarr"}


def test_a_setup_plane_path_in_the_range_is_flagged(tree: Tree):
    """Mixed planes: the setup half has its own arm, and this one must not claim it."""
    tree.write("ansible/roles/setup/k3s/defaults/main.yml", "k3s_x: 1\n")
    with pytest.raises(narrow_broad.CannotNarrow, match="setup"):
        tree.narrow(*_refs(tree))


def test_a_secret_rotation_beside_an_inventory_change_is_flagged(tree: Tree):
    """The clean half is `test_a_group_vars_key_two_roles_read_narrows_to_both`.

    The same inventory edit, plus a rotated secret. `secrets.yml` maps to no template, so
    the new value reaches a service only when that service renders again — and the full run
    this replaces rendered all of them.
    """
    tree.write("ansible/vars/secrets.yml", "sops: {}\n")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.2.0.0/24"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="secret"):
        tree.narrow(*_refs(tree))


def test_a_key_read_through_another_inventory_value_is_flagged(tree: Tree):
    """The clean half is `test_a_group_vars_key_two_roles_read_narrows_to_both`.

    `derived: "{{ unused_key }}"` makes `derived` a consumer of `unused_key`, and the key
    diff cannot see it: `derived`'s own parsed value is unchanged when `unused_key` moves.
    Without the inventory arm this narrowed to nothing, which the tick records as
    `narrowed-to-nothing` and `land.sh` reads as settled.
    """
    with_derived = GROUP_VARS + 'derived: "{{ unused_key }}"\n'
    tree.write("ansible/inventory/group_vars/all.yml", with_derived)
    tree.commit("a key consumed by another key")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        with_derived.replace("nobody-reads-this", "read-through-derived"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="another value"):
        tree.narrow(*_refs(tree))


def test_a_key_mentioned_only_in_an_example_inventory_file_narrows(tree: Tree):
    """A `_`-prefixed inventory file is loaded by no host, so it consumes nothing.

    `_inventory_tags` already skips one on the defining side. Reading it as a consumer
    refused eight live keys, `server_ip` and `has_igpu` among them. The reference here is
    UNCOMMENTED on purpose: a commented one would be swallowed by `_defines_only`'s own
    comment rule, and this test would pass with the `_` rule deleted.
    """
    tree.write(
        "ansible/inventory/host_vars/_example.yml",
        'derived: "{{ lan_subnet }}"\n',
    )
    tree.commit("an example host_vars file")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.4.0.0/24"),
    )
    assert tree.narrow(*_refs(tree)) == {"radarr", "sonarr"}


def test_a_key_named_only_in_a_comment_narrows(tree: Tree):
    """The rejecting half is `test_a_key_read_through_another_inventory_value_is_flagged`.

    `group_vars/all.yml` documents its own keys by name, so counting a comment line as a
    consumer refused 22 of its 87 top-level keys instead of 8.
    """
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        "# lan_subnet is the LAN, and nothing here reads it.\n" + GROUP_VARS,
    )
    tree.commit("a comment naming the key")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        ("# lan_subnet is the LAN, and nothing here reads it.\n" + GROUP_VARS).replace(
            "10.0.0.0/24", "10.5.0.0/24"
        ),
    )
    assert tree.narrow(*_refs(tree)) == {"radarr", "sonarr"}


def test_the_same_mention_in_a_loaded_inventory_file_still_refuses(tree: Tree):
    """The rejecting half: the exemption is the `_` prefix, not the host_vars directory."""
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS + 'derived: "{{ lan_subnet }}"\n',
    )
    tree.commit("a loaded consumer of the key")
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.4.0.0/24"),
    )
    with pytest.raises(narrow_broad.CannotNarrow, match="another value"):
        tree.narrow(*_refs(tree))


def test_a_key_reaching_an_uncallable_role_names_the_key(tree: Tree):
    """The refusal comes from `_role_tags`, which knows the role and not the variable.

    Both halves of the pair are here: the message has to carry the key, and the derivation
    line has to reach the journal, or the tick's log says a role refused and nothing says
    which inventory change reached it.
    """
    tree.write("ansible/roles/k8s/shared/templates/x.yaml.j2", "s: {{ shared_key }}\n")
    tree.write("ansible/inventory/group_vars/all.yml", GROUP_VARS + "shared_key: 1\n")
    tree.commit("a shared role nothing calls")
    tree.write("ansible/inventory/group_vars/all.yml", GROUP_VARS + "shared_key: 2\n")
    old, new = _refs(tree)
    lines: list[str] = []
    with pytest.raises(narrow_broad.CannotNarrow, match="shared_key") as exc:
        narrow_broad.narrow(
            old,
            new,
            cwd=tree.root,
            declared=DECLARED,
            callers={},
            explain=lines.append,
        )
    assert "no caller" in str(exc.value)
    assert "narrow: shared_key -> roles shared" in lines[-1]
