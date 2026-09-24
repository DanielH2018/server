"""What `narrow_setup.role_tags` narrows a setup-role change to, and what it refuses.

Every rule is a PAIR: one range it narrows to a tag list, and one it refuses with
`CannotNarrow`. A derivation that fired on everything and one that fired on nothing read
identically from the passing side alone.

`test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig` is the non-vacuity half. The
scan finds its subject by pattern — a task file's `src:` naming a template — so a renamed
template or a retagged task file would make it return an empty set that reads as "this
template reaches nothing" rather than as a broken scan. It names the concrete case #2307 was
filed for.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup.py
"""

import pytest

import narrow_setup
from lib.repo_paths import REPO

from _narrow_fixtures import Tree, _refs

ROLE = "ansible/roles/setup/demo"

# One task file per tag, the shape every setup role has: `main.yml` imports them and carries
# no tags of its own, so it is the untagged file the refusals key on.
MAIN = """\
---
- name: The first topic
  ansible.builtin.import_tasks: alpha.yml
- name: The second topic
  ansible.builtin.import_tasks: beta.yml
"""

ALPHA = """\
---
- name: Render the alpha config
  ansible.builtin.template:
    src: alpha.conf.j2
    dest: /etc/alpha.conf
  tags: [alpha]
- name: Restart alpha
  ansible.builtin.systemd:
    name: alpha
    state: restarted
  tags: [alpha]
"""

BETA = """\
---
- name: Render the beta config
  ansible.builtin.template:
    src: beta.conf.j2
    dest: /etc/beta.conf
  tags: [beta]
"""

DEFAULTS = """\
---
demo_alpha_mode: fast
demo_beta_mode: slow
demo_orphan_key: nobody-reads-this
"""


def build(tmp_path) -> Tree:
    """A checkout holding one setup role with two tagged task files and one untagged one."""
    tree = Tree(tmp_path / "repo")
    tree.write(f"{ROLE}/tasks/main.yml", MAIN)
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA)
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_beta_mode }}\n")
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS)
    tree.write(
        f"{ROLE}/handlers/main.yml", "---\n- name: noop\n  ansible.builtin.debug: {}\n"
    )
    tree.commit("base")
    return tree


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


def narrow(tree: Tree, old: str, new: str) -> frozenset[str]:
    return narrow_setup.role_tags("demo", "demo", old, new, str(tree.root))


# ── a template maps to the tags of the task file that renders it ────────────────────────


def test_a_template_change_narrows_to_its_own_task_files_tag(tree):
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha"})


def test_a_template_no_task_file_names_is_flagged(tree):
    tree.write(f"{ROLE}/templates/orphan.conf.j2", "nothing renders this\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="names orphan.conf.j2"):
        narrow(tree, *_refs(tree))


# ── a task file maps to its own tags, unless it has none ───────────────────────────────


def test_a_tagged_task_file_change_narrows_to_its_tags(tree):
    tree.write(f"{ROLE}/tasks/beta.yml", BETA + "  # touched\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})


def test_an_untagged_task_file_change_is_flagged(tree):
    tree.write(f"{ROLE}/tasks/main.yml", MAIN + "# touched\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="carries no tags of its own"):
        narrow(tree, *_refs(tree))


# ── a defaults key maps to the tags of whatever reads it ───────────────────────────────


def test_a_changed_defaults_key_narrows_to_its_readers_tags(tree):
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS.replace("fast", "faster"))
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha"})


def test_a_changed_defaults_key_nothing_reads_is_flagged(tree):
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS.replace("nobody-reads-this", "x"))
    with pytest.raises(narrow_setup.CannotNarrow, match="reads demo_orphan_key"):
        narrow(tree, *_refs(tree))


def test_a_defaults_file_the_range_adds_is_flagged(tree):
    tree.write(f"{ROLE}/vars/main.yml", "---\ndemo_alpha_mode: fast\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="is absent at"):
        narrow(tree, *_refs(tree))


# ── everything else in the role refuses ────────────────────────────────────────────────


def test_a_handlers_change_is_flagged(tree):
    tree.write(
        f"{ROLE}/handlers/main.yml",
        "---\n- name: noop\n  ansible.builtin.debug: {}\n# touched\n",
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="not in a directory"):
        narrow(tree, *_refs(tree))


def test_a_range_touching_two_topics_names_both_tags(tree):
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    tree.write(f"{ROLE}/tasks/beta.yml", BETA + "  # touched\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha", "beta"})


def test_a_derivation_landing_on_the_role_tag_is_flagged(tree):
    """A tag equal to the whole-role tag has narrowed nothing, so it must refuse."""
    tree.write(f"{ROLE}/tasks/beta.yml", BETA.replace("[beta]", "[demo]"))
    with pytest.raises(narrow_setup.CannotNarrow, match="whole-role tag"):
        narrow(tree, *_refs(tree))


# ── the real tree: the case #2307 names, so the scan cannot go vacuous ─────────────────


def test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig():
    index = narrow_setup.RoleIndex("k3s", "HEAD", str(REPO))
    assert index.readers_of("readonly-rbac.yaml.j2") == frozenset({"kubeconfig"})
    assert index.key_readers("k3s_readonly_crd_api_groups") == frozenset({"kubeconfig"})


def test_the_real_k3s_roles_untagged_task_files_are_named_as_such():
    """`main.yml` and the two files imported under another file's tags carry no tags.

    Named rather than counted: a file that gained its own tags is a real change to what
    `--tags` selects, and the derivation's refusals depend on which files are untagged.
    """
    index = narrow_setup.RoleIndex("k3s", "HEAD", str(REPO))
    untagged = {rel for rel, tags in index.tags.items() if tags is None}
    assert untagged == {
        "tasks/main.yml",
        "tasks/unit-logging.yml",
        "tasks/longhorn-weekly-shard.yml",
    }
