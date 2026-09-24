"""The `defaults/` and `vars/` walk `narrow_setup` does over parsed values (#2371).

Split out of `test_narrow_setup_edges.py` at its 500-line cap. The walk replaced a line scan,
so these pin the YAML shapes the scan lost, the vars files `RoleIndex` must parse or refuse
on, and the cycles one step in that used to merge an empty answer — each as a clean/flagged
pair.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_vars_walk.py
"""

import pytest

import narrow_setup

from _setup_role_fixtures import DEFAULTS, ROLE, Tree, build, narrow


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
