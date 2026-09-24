"""The scan edges `narrow_setup` walks under its stated rules, each as a clean/flagged pair.

`test_narrow_setup.py` covers the rules `role_tags` names in its own docstring. This module
covers the edges underneath them, which had no test of either polarity before #2347: the
template-include edge, a `files/` name, a `vars/<f>.yml` key, a tag a `block:` carries for
tasks that carry none, and a task file that does not parse. #2344's union (a template named in
a task's `src:` AND in a `defaults/` structure) and #2350's two tag refusals (a special tag, a
tag another role in the same playbook declares) are here for the same reason — a scan that
stopped matching would still read green from the accepting side alone.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_edges.py
"""

import pytest

import narrow_setup

from _narrow_fixtures import _refs
from _setup_role_fixtures import ALPHA, DEFAULTS, MAIN, ROLE, Tree, build, narrow

DEFAULTS_WITH_A_DERIVED_KEY = DEFAULTS + 'demo_derived: "x{{ demo_alpha_mode }}"\n'


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


# ── a template another template includes maps to the outer one's readers ────────────────


def test_a_template_an_included_template_names_narrows_to_the_outer_ones_tag(tree):
    """`_TEMPLATE_EDGE`'s accepting half: the edge is followed, not stopped at."""
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2",
        "{% include 'alpha-part.j2' %}\nmode = {{ demo_alpha_mode }}\n",
    )
    tree.write(f"{ROLE}/templates/alpha-part.j2", "part\n")
    old = tree.commit("include a part")
    tree.write(f"{ROLE}/templates/alpha-part.j2", "part, edited\n")
    assert narrow(tree, old, tree.commit("edit the part")) == frozenset({"alpha"})


def test_a_template_included_only_by_a_template_no_task_renders_is_flagged(tree):
    """The edge ends at a template nothing renders, which is a refusal, not an empty set."""
    tree.write(f"{ROLE}/templates/outer.j2", "{% include 'inner.j2' %}\n")
    tree.write(f"{ROLE}/templates/inner.j2", "x\n")
    old = tree.commit("an unrendered pair")
    tree.write(f"{ROLE}/templates/inner.j2", "y\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="names outer.j2"):
        narrow(tree, old, tree.commit("edit inner"))


# ── a `files/` path maps to the task file naming it ─────────────────────────────────────

ALPHA_COPIES_A_FILE = (
    ALPHA + "- name: Install the alpha script\n"
    "  ansible.builtin.copy:\n"
    "    src: alpha.sh\n"
    "    dest: /usr/local/bin/alpha\n"
    "  tags: [alpha]\n"
)


def test_a_files_path_narrows_to_the_task_file_naming_it(tree):
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_COPIES_A_FILE)
    tree.write(f"{ROLE}/files/alpha.sh", "#!/bin/sh\n")
    old = tree.commit("ship a script")
    tree.write(f"{ROLE}/files/alpha.sh", "#!/bin/sh\necho alpha\n")
    assert narrow(tree, old, tree.commit("edit the script")) == frozenset({"alpha"})


def test_a_files_path_no_task_file_names_is_flagged(tree):
    tree.write(f"{ROLE}/files/orphan.sh", "#!/bin/sh\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="names orphan.sh"):
        narrow(tree, *_refs(tree))


# ── a `vars/<f>.yml` key narrows like a `defaults/` one ─────────────────────────────────

ALPHA_READS_A_VARS_KEY = (
    ALPHA + "- name: Use the extra\n"
    "  ansible.builtin.debug:\n"
    '    msg: "{{ demo_alpha_extra }}"\n'
    "  tags: [alpha]\n"
)


def test_a_changed_vars_key_narrows_to_its_readers_tags(tree):
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_READS_A_VARS_KEY)
    tree.write(f"{ROLE}/vars/extra.yml", "---\ndemo_alpha_extra: 1\n")
    old = tree.commit("a vars file with a reader")
    tree.write(f"{ROLE}/vars/extra.yml", "---\ndemo_alpha_extra: 2\n")
    assert narrow(tree, old, tree.commit("change the vars key")) == frozenset({"alpha"})


def test_a_changed_vars_key_nothing_reads_is_flagged(tree):
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_READS_A_VARS_KEY)
    tree.write(
        f"{ROLE}/vars/extra.yml", "---\ndemo_alpha_extra: 1\ndemo_vars_orphan: a\n"
    )
    old = tree.commit("a vars file with an unread key")
    tree.write(
        f"{ROLE}/vars/extra.yml", "---\ndemo_alpha_extra: 1\ndemo_vars_orphan: b\n"
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="reads demo_vars_orphan"):
        narrow(tree, old, tree.commit("change the unread key"))


# ── a key another defaults key interpolates reaches that key's readers too ──────────────


def test_a_key_another_defaults_key_interpolates_names_both_readers(tree):
    """`k3s_node_dns_options` interpolates `k3s_node_dns_timeout`: the real role's shape.

    `demo_derived` carries `demo_alpha_mode` into `beta.conf.j2`, so a change to the mode
    reaches `beta` as surely as `alpha`. Scanning only task files and templates returned
    `alpha` alone, and the operator cleared the marker over an unapplied `beta` change.
    """
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS_WITH_A_DERIVED_KEY)
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_derived }}\n")
    old = tree.commit("derive a key from the alpha mode")
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS_WITH_A_DERIVED_KEY.replace("mode: fast", "mode: faster"),
    )
    assert narrow(tree, old, tree.commit("change the mode")) == frozenset(
        {"alpha", "beta"}
    )


def test_keys_only_naming_each_other_are_flagged_beside_a_key_that_narrows(tree):
    """The fail-closed half: a key cycle reaching no task file refuses per key.

    It must not simply drop out of the union beside `demo_alpha_mode`, which does narrow.
    """
    cyclic = DEFAULTS + 'demo_ping: "{{ demo_pong }}"\ndemo_pong: "{{ demo_ping }}"\n'
    tree.write(f"{ROLE}/defaults/main.yml", cyclic)
    old = tree.commit("two keys naming each other")
    tree.write(
        f"{ROLE}/defaults/main.yml",
        cyclic.replace("mode: fast", "mode: faster").replace(
            'demo_ping: "{{ demo_pong }}"', 'demo_ping: "x{{ demo_pong }}"'
        ),
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="demo_ping reaches no task"):
        narrow(tree, old, tree.commit("change both"))


def test_a_template_listed_in_a_key_cycle_is_flagged_beside_its_src_reader(tree):
    """The same fail-closed half through `readers_of`, where the template is named directly.

    `alpha.yml`'s `src:` reads the template, and `demo_group` lists it too. `demo_group` is
    read only by a key it names back, so that half reaches no task file and must refuse
    rather than leave `alpha` standing as the whole answer.
    """
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS
        + 'demo_group:\n  via: "{{ demo_echo }}"\n  templates:\n    - alpha.conf.j2\n'
        + 'demo_echo: "{{ demo_group }}"\n',
    )
    old = tree.commit("list the template in a key cycle")
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="demo_group reaches no task"):
        narrow(tree, old, tree.commit("edit the template"))


# ── a tag a `block:` carries selects the tasks inside it ────────────────────────────────

DELTA_TAGGED_BLOCK = """\
---
- name: A grouped topic
  tags: [delta]
  block:
    - name: The work the block tags
      ansible.builtin.debug:
        msg: delta
"""

DELTA_UNTAGGED_BLOCK = """\
---
- name: A grouped topic
  block:
    - name: The work nothing tags
      ansible.builtin.debug:
        msg: delta
"""

MAIN_IMPORTS_DELTA = MAIN + (
    "- name: The third topic\n  ansible.builtin.import_tasks: delta.yml\n"
)


def _with_delta(tree: Tree, delta: str) -> str:
    """Commit a role whose `main.yml` imports `tasks/delta.yml`, and return that commit."""
    tree.write(f"{ROLE}/tasks/main.yml", MAIN_IMPORTS_DELTA)
    tree.write(f"{ROLE}/tasks/delta.yml", delta)
    return tree.commit("import delta")


def test_a_tag_carried_only_by_a_block_narrows_to_that_tag(tree):
    old = _with_delta(tree, DELTA_TAGGED_BLOCK)
    tree.write(f"{ROLE}/tasks/delta.yml", DELTA_TAGGED_BLOCK + "# touched\n")
    assert narrow(tree, old, tree.commit("touch delta")) == frozenset({"delta"})


def test_a_block_no_tag_reaches_is_flagged(tree):
    old = _with_delta(tree, DELTA_UNTAGGED_BLOCK)
    tree.write(f"{ROLE}/tasks/delta.yml", DELTA_UNTAGGED_BLOCK + "# touched\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="carries no tags of its own"):
        narrow(tree, old, tree.commit("touch delta"))


# ── a task file that does not parse refuses the whole narrowing ─────────────────────────


def test_a_task_file_that_parses_narrows(tree):
    """The accepting half of the pair below: the same edit, well-formed."""
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA + "# a trailing comment\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha"})


def test_a_task_file_that_does_not_parse_is_flagged(tree):
    """`RoleIndex` parses every task file in the role, so one broken file refuses all of it.

    Asserted through `role_tags` rather than `file_tags`: the refusal an operator meets comes
    from the index build, which reads files the changed path never named.
    """
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA + "  bad: [unclosed\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="does not parse"):
        narrow(tree, *_refs(tree))


# ── a template named in both a `src:` and a `defaults/` structure names both tags (#2344) ─

DEFAULTS_NAMING_ALPHA_CONF = """\
---
demo_alpha_mode: fast
demo_beta_mode: slow
demo_release_groups:
  - name: demo-beta
    templates:
      - alpha.conf.j2
"""

DEFAULTS_WITH_AN_UNREAD_GROUP = """\
---
demo_alpha_mode: fast
demo_beta_mode: slow
demo_unread_groups:
  - name: demo-orphan
    templates:
      - alpha.conf.j2
"""


def test_a_template_named_in_a_src_and_in_a_defaults_structure_names_both_tags(tree):
    """`readers_of` unions the two kinds of hit rather than stopping at the first (#2344).

    `alpha.conf.j2` is `alpha.yml`'s `src:` AND a member of `demo_release_groups`, which
    `beta.yml` renders through its `release_bin.yml` import. Returning `alpha` alone left the
    operator clearing the marker over a partly applied change.
    """
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS_NAMING_ALPHA_CONF)
    old = tree.commit("name alpha.conf.j2 in a group too")
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    assert narrow(tree, old, tree.commit("edit the template")) == frozenset(
        {"alpha", "beta"}
    )


def test_a_template_named_in_a_defaults_key_nothing_reads_is_flagged(tree):
    """The fail-closed half: a key-reader that is missing still refuses after the union."""
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS_WITH_AN_UNREAD_GROUP)
    old = tree.commit("name it in a group nothing reads")
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="reads demo_unread_groups"):
        narrow(tree, old, tree.commit("edit the template"))


# ── a special tag and a tag another role declares are both refused (#2350) ──────────────


def test_a_second_ordinary_tag_on_a_task_file_narrows_to_both(tree):
    tree.write(
        f"{ROLE}/tasks/alpha.yml", ALPHA.replace("[alpha]", "[alpha, alpha-cron]")
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha", "alpha-cron"})


def test_a_task_file_carrying_never_is_flagged(tree):
    """`docker_install`'s `tasks/install.yml` is the real one: `never` beside its own tag.

    Printing it would tell an operator to select the opt-in tasks a config edit never asked
    for, so the derivation refuses and the caller prints the whole-role tag.
    """
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA.replace("[alpha]", "[alpha, never]"))
    with pytest.raises(narrow_setup.CannotNarrow, match="special tag never"):
        narrow(tree, *_refs(tree))


TWO_ROLE_PLAYBOOK = "ansible/two.yml"
TWO_ROLE_PLAYBOOK_TEXT = """\
---
- name: Apply both roles
  hosts: localhost
  roles:
    - { role: demo, tags: ["demo"] }
    - { role: other, tags: ["other"] }
"""


def _two_role_tree(tree: Tree, other_tag: str) -> str:
    """Commit a second role in the same playbook whose tasks carry `other_tag`."""
    tree.write(TWO_ROLE_PLAYBOOK, TWO_ROLE_PLAYBOOK_TEXT)
    tree.write(
        "ansible/roles/setup/other/tasks/main.yml",
        "---\n- name: The other role's firewall\n"
        "  ansible.builtin.debug:\n    msg: other\n"
        f"  tags: [{other_tag}]\n",
    )
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA.replace("[alpha]", "[firewall]"))
    return tree.commit("a second role in the playbook")


def _narrow_two(tree: Tree, old: str, new: str) -> frozenset[str]:
    return narrow_setup.role_tags(
        "demo", "demo", old, new, str(tree.root), TWO_ROLE_PLAYBOOK
    )


def test_a_tag_only_this_role_declares_narrows(tree):
    old = _two_role_tree(tree, "other-only")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    assert _narrow_two(tree, old, tree.commit("edit alpha")) == frozenset({"firewall"})


def test_a_tag_another_role_in_the_playbook_declares_is_flagged(tree):
    """`firewall` is carried by tasks in both `deploy_ui` and `initial_setup`.

    `initial_setup.yml --tags firewall` runs both roles' firewall tasks, so the printed
    command would apply a role the change never touched.
    """
    old = _two_role_tree(tree, "firewall")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    with pytest.raises(
        narrow_setup.CannotNarrow, match="also declared by another role"
    ):
        _narrow_two(tree, old, tree.commit("edit alpha"))


def test_the_real_setup_tree_has_a_tag_two_roles_declare():
    """Non-vacuity: the rule above has a live subject, so it is not guarding an empty set.

    `foreign_tags` finds its subject by pattern — every other role the playbook lists — and
    would return an empty set, silently, if the playbook parse or the tree walk broke.
    """
    from lib.repo_paths import REPO

    text = (REPO / "ansible/initial_setup.yml").read_text()
    shared = narrow_setup.foreign_tags("initial_setup", text, "HEAD", str(REPO))
    assert "firewall" in shared, sorted(shared)
