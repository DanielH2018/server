"""The `defaults/` and `vars/` walk `narrow_setup` does over parsed values (#2371).

Split out of `test_narrow_setup_edges.py` at its 500-line cap. The walk replaced a line scan,
so these pin the YAML shapes the scan lost, the vars files `RoleIndex` must parse or refuse
on, and the cycles one step in that used to merge an empty answer — each as a clean/flagged
pair.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_vars_walk.py
"""

import pytest

from deployer_lib import deploy_narrow
import narrow_setup
from lib.repo_paths import REPO

from _narrow_fixtures import _refs
from _setup_role_fixtures import (
    ALPHA,
    BETA,
    DEFAULTS,
    PLAYBOOK,
    ROLE,
    Tree,
    build,
    narrow,
    touched,
)


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


# ── two YAML shapes the line scan lost the key of (#2371) ───────────────────────────────

DEFAULTS_WITH_A_BLOCK_SCALAR = DEFAULTS + (
    "demo_script: |\n  #!/bin/sh\n\n  echo {{ demo_alpha_mode }}\n"
)

DEFAULTS_WITH_AN_ALIAS = DEFAULTS + ('demo_a: &a "{{ demo_alpha_mode }}"\ndemo_b: *a\n')


def test_a_key_interpolated_after_a_blank_line_in_a_block_scalar_names_its_readers(
    tree,
):
    """A blank line inside a block scalar ended the key's block for the line scan.

    Everything after it was attributed to no key at all, so `demo_script` did not read
    `demo_alpha_mode` and the change narrowed to `alpha` alone while `beta.conf.j2` rendered
    the script.
    """
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS_WITH_A_BLOCK_SCALAR)
    tree.write(f"{ROLE}/templates/beta.conf.j2", "script = {{ demo_script }}\n")
    old = tree.commit("a block scalar holding a blank line")
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS_WITH_A_BLOCK_SCALAR.replace("mode: fast", "mode: faster"),
    )
    assert narrow(tree, old, tree.commit("change the mode")) == frozenset(
        {"alpha", "beta"}
    )


def test_a_key_reached_through_a_yaml_alias_names_its_readers(tree):
    """An alias copies a value without repeating the text that named anything.

    `demo_b: *a` holds the same `{{ demo_alpha_mode }}` string as `demo_a`, which the line
    scan could not see, so `beta.conf.j2` dropped out of the answer.
    """
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS_WITH_AN_ALIAS)
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_a }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_b }}\n")
    old = tree.commit("an alias of the alpha mode")
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS_WITH_AN_ALIAS.replace("mode: fast", "mode: faster"),
    )
    assert narrow(tree, old, tree.commit("change the mode")) == frozenset(
        {"alpha", "beta"}
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param("---\nbad: [unclosed\n", "does not parse", id="does_not_parse"),
        pytest.param(
            "---\n- not\n- a mapping\n", "is not a mapping of keys", id="not_a_mapping"
        ),
    ],
)
def test_a_vars_file_the_index_cannot_read_beside_a_changed_template_is_flagged(
    tree, text, match
):
    """The index's rejecting half: an unreadable vars file is a reader it cannot see.

    The changed path is a template, so `changed_keys` never runs and only `RoleIndex`
    parsing `vars/extra.yml` can refuse. Before #2371 this narrowed to `alpha`.
    """
    tree.write(f"{ROLE}/vars/extra.yml", text)
    old = tree.commit("an unreadable vars file")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} #\n")
    with pytest.raises(narrow_setup.CannotNarrow, match=f"vars/extra.yml {match}"):
        narrow(tree, old, tree.commit("edit alpha"))


@pytest.mark.parametrize(
    ("before", "match"),
    [
        pytest.param(DEFAULTS + "  bad: [unclosed\n", "does not parse", id="parse"),
        pytest.param("---\n- a\n- list\n", "is not a mapping of keys", id="mapping"),
    ],
)
def test_a_changed_defaults_file_unreadable_before_the_range_is_flagged(
    tree, before, match
):
    """`changed_keys`'s rejecting half: a before-state it cannot read has no keys to diff.

    The file is valid at `new`, which is the ref `RoleIndex` reads, so only `changed_keys`
    parsing `old` can refuse.
    """
    tree.write(f"{ROLE}/defaults/main.yml", before)
    old = tree.commit("an unreadable defaults file")
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS)
    with pytest.raises(narrow_setup.CannotNarrow, match=f"main.yml {match}"):
        narrow(tree, old, tree.commit("repair the defaults"))


def test_a_recursive_alias_in_a_changed_vars_file_is_flagged(tree):
    """A self-referencing value compares by recursing forever, so it refuses instead.

    Comparing `demo_loop` with itself raised `RecursionError` out of `changed_keys` rather
    than the `CannotNarrow` every caller handles.
    """
    looped = DEFAULTS + "demo_loop: &loop [1, *loop]\n"
    tree.write(f"{ROLE}/defaults/main.yml", looped)
    old = tree.commit("a recursive alias")
    tree.write(
        f"{ROLE}/defaults/main.yml", looped.replace("mode: fast", "mode: faster")
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="recursive"):
        narrow(tree, old, tree.commit("change the mode"))


@pytest.mark.parametrize("name", ["main.json", "main"])
def test_a_json_or_extensionless_vars_file_names_its_readers(tree, name):
    """Ansible loads a role's `vars/main.json` and an extensionless `vars/main` too.

    `Role._load_role_yaml` tries `.yml`, `.yaml`, `.json` and no extension, so a key there
    that interpolates the changed one is a reader like any other. Skipping those files
    dropped `beta` from the answer.
    """
    tree.write(f"{ROLE}/vars/{name}", '{"demo_derived": "x{{ demo_alpha_mode }}"}\n')
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_derived }}\n")
    old = tree.commit("a derived key outside YAML")
    tree.write(
        f"{ROLE}/defaults/main.yml", DEFAULTS.replace("mode: fast", "mode: faster")
    )
    assert narrow(tree, old, tree.commit("change the mode")) == frozenset(
        {"alpha", "beta"}
    )


def test_a_non_yaml_file_beside_the_defaults_does_not_block_the_narrowing(tree):
    """A README is not a vars file: Ansible loads `.yml`, `.yaml`, `.json` and bare names."""
    tree.write(f"{ROLE}/defaults/README.md", "# not a vars file\n")
    old = tree.commit("prose beside the defaults")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    assert narrow(tree, old, tree.commit("edit alpha")) == frozenset({"alpha"})


# ── a cycle one step in refuses rather than merging an empty answer (#2371) ──────────────


def test_a_reader_key_whose_own_readers_form_a_cycle_is_flagged(tree):
    """`demo_y` reads the changed key and reaches only `demo_z`, which names it back.

    `key_readers` unioned that empty answer silently, so the range narrowed to `alpha` and
    read as complete. The accepting half is
    `test_a_key_another_defaults_key_interpolates_names_both_readers` in the edges module,
    where the same derived key does reach a task file.
    """
    cyclic = DEFAULTS + (
        'demo_y: "{{ demo_alpha_mode }}{{ demo_z }}"\ndemo_z: "{{ demo_y }}"\n'
    )
    tree.write(f"{ROLE}/defaults/main.yml", cyclic)
    old = tree.commit("a derived key reaching only a cycle")
    tree.write(
        f"{ROLE}/defaults/main.yml", cyclic.replace("mode: fast", "mode: faster")
    )
    with pytest.raises(
        narrow_setup.CannotNarrow, match="demo_y reaches no task file through demo_z"
    ):
        narrow(tree, old, tree.commit("change the mode"))


def test_a_template_reader_in_a_cycle_is_flagged_beside_the_src_reader(tree):
    """The same silent merge through `readers_of`'s template edge.

    `outer.j2` names `alpha.conf.j2` and is itself rendered by nothing — it only trades
    includes with `inner.j2`. Returning `alpha` alone dropped whatever `outer.j2` reaches.
    """
    tree.write(
        f"{ROLE}/templates/outer.j2",
        "{% include 'inner.j2' %}\n{% include 'alpha.conf.j2' %}\n",
    )
    tree.write(f"{ROLE}/templates/inner.j2", "{% include 'outer.j2' %}\n")
    old = tree.commit("a template pair rendering nothing")
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    with pytest.raises(
        narrow_setup.CannotNarrow, match="only templates naming each other"
    ):
        narrow(tree, old, tree.commit("edit the template"))


# ── another role's defaults key this role reads maps to the key's readers (#4303) ───────

FOREIGN = "ansible/roles/setup/owner/defaults/main.yml"
FOREIGN_DEFAULTS = "---\nowner_group: holders\nowner_other: unread\n"


@pytest.fixture
def reads(tree, monkeypatch) -> Tree:
    """The demo role declared a reader of `FOREIGN`, whose `owner_group` its alpha template names."""
    monkeypatch.setitem(
        narrow_setup.deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
        FOREIGN,
        frozenset({"demo"}),
    )
    tree.write(FOREIGN, FOREIGN_DEFAULTS)
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2",
        "mode = {{ demo_alpha_mode }}\ngroup = {{ owner_group }}\n",
    )
    tree.commit("demo reads the owner's group")
    return tree


def test_a_foreign_defaults_key_the_role_reads_narrows_to_its_readers(reads):
    reads.write(FOREIGN, FOREIGN_DEFAULTS.replace("holders", "renamed"))
    assert narrow(reads, *_refs(reads)) == frozenset({"alpha"})


def test_a_foreign_defaults_key_the_role_never_reads_adds_no_tag(reads):
    reads.write(FOREIGN, FOREIGN_DEFAULTS.replace("unread", "changed"))
    reads.write(f"{ROLE}/tasks/beta.yml", touched(BETA))
    assert narrow(reads, *_refs(reads)) == frozenset({"beta"})


def test_a_range_changing_only_an_unread_foreign_key_exits_with_the_deployers_nothing_pair(
    reads, capsys
):
    """The role needs no tag, said as an exit code and a line, never as nothing (#4361).

    An empty `--tags` value runs the whole playbook. Driven through the deployer's own argv,
    so `narrow_setup.NOTHING_EXIT` drifting from `deploy_narrow.NARROW_SETUP_NOTHING_EXIT`,
    which cannot import each other, fails here.
    """
    reads.write(FOREIGN, FOREIGN_DEFAULTS.replace("unread", "changed"))
    old, new = _refs(reads)
    argv = deploy_narrow.narrow_setup_argv("demo", "demo", PLAYBOOK, old, new)
    rc = narrow_setup.main([*argv[5:], "--repo", str(reads.root)])
    assert (rc, capsys.readouterr().out.strip()) == (
        deploy_narrow.NARROW_SETUP_NOTHING_EXIT,
        deploy_narrow.NARROWED_TO_NOTHING,
    )


# A name built from `owner_other`, in each place a value can be carried onward from.
LABEL = 'owner_label: "{{ owner_other }}-x"\n'
INVENTORY_ALL = "ansible/inventory/group_vars/all.yml"


@pytest.mark.parametrize(
    ("carriers", "name"),
    [
        ({FOREIGN: FOREIGN_DEFAULTS + LABEL}, "owner_label"),
        ({INVENTORY_ALL: "---\n" + LABEL}, "owner_label"),
        (
            {
                "ansible/roles/setup/owner/tasks/main.yml": "---\n- name: Derive the label\n"
                "  ansible.builtin.set_fact:\n    " + LABEL
            },
            "owner_label",
        ),
        # Two hops across two carriers, the shape daniel-box's `claude_code_user` override
        # puts between `claude_code_agent_user` and `claude_code_rc_workdir`.
        (
            {
                INVENTORY_ALL: "---\n" + LABEL,
                FOREIGN: FOREIGN_DEFAULTS + 'owner_path: "{{ owner_label }}/y"\n',
            },
            "owner_path",
        ),
    ],
    ids=["owner-key", "inventory-value", "owner-fact", "two-hops"],
)
def test_a_foreign_key_the_role_reads_through_another_name_narrows_to_its_readers(
    reads, carriers, name
):
    """The role names only `name`, which is built from the changed `owner_other`.

    Skipping `owner_other` as unread would answer that the role needs nothing, and nothing
    would apply the new value of `name`.
    """
    for carrier, text in carriers.items():
        reads.write(carrier, text)
    reads.write(
        f"{ROLE}/templates/beta.conf.j2",
        f"mode = {{{{ demo_beta_mode }}}}\nlabel = {{{{ {name} }}}}\n",
    )
    reads.commit(f"demo reads {name}, built from owner_other")
    foreign = (reads.root / FOREIGN).read_text()
    reads.write(FOREIGN, foreign.replace("unread", "changed"))
    assert narrow(reads, *_refs(reads)) == frozenset({"beta"})


def test_a_foreign_key_hosts_ini_names_is_flagged(reads):
    """`hosts.ini` can set a host variable inline, which the walk cannot follow."""
    reads.write(
        "ansible/inventory/hosts.ini", "[all]\nlocalhost label={{ owner_other }}\n"
    )
    reads.commit("hosts.ini carries owner_other")
    reads.write(FOREIGN, FOREIGN_DEFAULTS.replace("unread", "changed"))
    with pytest.raises(narrow_setup.CannotNarrow, match="not a YAML mapping"):
        narrow(reads, *_refs(reads))


# ── a vars edit that changes no parsed value (#4384) ────────────────────────────────────


def test_a_comment_edit_to_defaults_beside_a_template_edit_narrows_to_the_template(
    tree,
):
    """A comment in `defaults/main.yml` reaches no host, so it adds no tag and refuses nothing."""
    tree.write(
        f"{ROLE}/defaults/main.yml", DEFAULTS + "# a comment an operator reads\n"
    )
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # v2\n")
    old = tree.commit("base")
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS + "# the comment, reworded\n")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # v3\n")
    assert narrow(tree, old, tree.commit("comment and template")) == frozenset(
        {"alpha"}
    )


def test_a_comment_edit_to_defaults_alone_needs_nothing(tree):
    """An empty `--tags` runs the whole playbook, so the answer is `NothingToApply` (#4361)."""
    tree.write(f"{ROLE}/defaults/main.yml", DEFAULTS + "# the comment, reworded\n")
    with pytest.raises(narrow_setup.NothingToApply, match="reaches no host"):
        narrow(tree, *_refs(tree))


@pytest.mark.parametrize(
    ("role", "tags"),
    [
        ("homelab_builder", {"homelab-builder-install"}),
        ("renovate_agent", {"renovate-agent-code", "renovate-agent-service"}),
        ("claude_code", {"claude_code"}),
    ],
)
def test_the_real_uv_pin_reaches_every_role_copying_the_operators_uv(role, tags):
    """Each role copies the uv initial_setup pins, so a bump of the pin keeps its copy's tag
    (#5945). Before, a uv bump narrowed claude_code to nothing and never reached the other
    two, and all three copies stayed on the old version."""
    index = narrow_setup.RoleIndex(role, "HEAD", str(REPO))
    assert index.key_readers("initial_setup_uv_version") == frozenset(tags)


# ── a key or path the range deletes together with its last reader (#5930) ──────────────
#
# Each clean test reads the deleted path's own line from stderr. The range's union cannot show
# where a tag came from: the reader the range edits contributes the same tag, so skipping the
# deleted path outright would return the same set.


def test_a_key_deleted_with_its_last_reader_narrows_to_that_readers_tag(tree, capsys):
    """H1.4 deleted `k3s_cluster_dns_ip` with the coredns.yml tasks that read it."""
    tree.write(
        f"{ROLE}/defaults/main.yml", DEFAULTS.replace("demo_beta_mode: slow\n", "")
    )
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = slow\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})
    assert "narrow-setup: defaults/main.yml -> beta\n" in capsys.readouterr().err


def test_a_key_deleted_with_the_last_task_carrying_its_readers_tag_is_flagged(tree):
    """The old reader's tag selects nothing at the new commit, so it cannot be printed."""
    tree.write(
        f"{ROLE}/defaults/main.yml", DEFAULTS.replace("demo_alpha_mode: fast\n", "")
    )
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = fast\n")
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA.replace("[alpha]", "[alpha_v2]"))
    with pytest.raises(narrow_setup.CannotNarrow, match="which no task carries"):
        narrow(tree, *_refs(tree))


def test_a_template_deleted_with_its_last_reader_narrows_to_that_readers_tag(
    tree, capsys
):
    """H1.4 deleted `templates/coredns-corefile.j2`, which nothing names at the new commit."""
    (tree.root / ROLE / "templates/beta.conf.j2").unlink()
    tree.write(
        f"{ROLE}/tasks/beta.yml",
        BETA.replace("template:\n    src: beta.conf.j2", "copy:\n    content: slow"),
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})
    assert "narrow-setup: templates/beta.conf.j2 -> beta\n" in capsys.readouterr().err


def test_a_template_deleted_with_the_last_task_carrying_its_readers_tag_is_flagged(
    tree,
):
    (tree.root / ROLE / "templates/alpha.conf.j2").unlink()
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA.replace("[alpha]", "[alpha_v2]").replace("alpha.conf.j2", "other.j2"),
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="which no task carries"):
        narrow(tree, *_refs(tree))


# A task file holding a second tag's task beside its own, the shape `stale-checkout-guard.yml`
# has: one import per writer tag. `beta` stays live in `beta.yml` (#6234).
BETA_IN_ALPHA = """\
- name: Restart beta
  ansible.builtin.systemd:
    name: beta
    state: restarted
  tags: [beta]
"""


def _mixed(tree: Tree, extra: str = "") -> None:
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA + BETA_IN_ALPHA + extra)
    tree.commit("mixed")


def test_a_task_deleted_beside_survivors_narrows_to_its_own_tag(tree, capsys):
    """H1.4 deleted the `coredns` guard instance and left the Longhorn ones."""
    _mixed(tree)
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA)
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})
    assert "narrow-setup: tasks/alpha.yml -> beta\n" in capsys.readouterr().err


def test_a_task_edited_beside_unchanged_ones_narrows_to_its_own_tag(tree):
    _mixed(tree)
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA + BETA_IN_ALPHA.replace("restarted", "reloaded"),
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})


def test_a_deleted_task_whose_tag_no_task_carries_any_more_is_flagged(tree):
    _mixed(tree)
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA.replace("[beta]", "[beta_v2]"))
    with pytest.raises(narrow_setup.CannotNarrow, match="which no task carries"):
        narrow(tree, *_refs(tree))


@pytest.mark.parametrize(
    "loader",
    [
        "  ansible.builtin.set_fact:\n    demo_fact: 1\n",
        "  ansible.builtin.include_vars:\n    file: extra.yml\n",
        "  ansible.builtin.command: /bin/true\n  register: demo_probe\n",
    ],
    ids=["set_fact", "include_vars", "register"],
)
def test_a_file_that_sets_variables_keeps_every_tasks_tag(tree, loader):
    """An edited task may read what an unchanged one sets under another tag."""
    _mixed(tree, f"- name: Load the demo variables\n{loader}  tags: [alpha]\n")
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA
        + BETA_IN_ALPHA.replace("restarted", "reloaded")
        + f"- name: Load the demo variables\n{loader}  tags: [alpha]\n",
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha", "beta"})


def test_the_real_h1_4_range_narrows_below_the_k3s_role_tag(capsys):
    """The range whose `manual_plane` line recorded `"tags": []` (#5930), from history.

    The range deleted only the `coredns` instance from `stale-checkout-guard.yml`. Before the
    per-task diff (#6234) the surviving Longhorn instances' tags stood in for it.
    """
    got = narrow_setup.role_tags(
        "k3s", "k3s", "225de93f5^", "225de93f5", str(REPO), "ansible/k3s-bringup.yml"
    )
    assert got == frozenset({"coredns"})
    err = capsys.readouterr().err
    assert "narrow-setup: tasks/stale-checkout-guard.yml -> coredns\n" in err
    assert "narrow-setup: defaults/main.yml -> coredns\n" in err
    assert "narrow-setup: templates/coredns-corefile.j2 -> coredns\n" in err
