"""The `set_fact` edge `narrow_setup` follows, as a clean/flagged pair plus a tree census.

`set_fact` writes a host variable that outlives the task file that set it, so a value derived
under one tag and read under another is work both tags apply. Before #2384 the scans saw the
name in both files and nothing tied the two, so a change reaching only the setter narrowed to
the setter's tags and the consumer's were dropped — the one direction the derivation must
never fail in, because the printed `--tags` value then leaves the work merged and unapplied
while the operator clears the `manual_plane` marker over it.

`test_the_fact_edge_widens_no_real_setup_role_today` is the census #2384 asked for, and
`K3S_FACTS` names `setup/k3s`'s two facts beside it — so the day one of them moves to a
differently tagged file the tree says so rather than the narrowing widening quietly.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_facts.py
"""

import pytest

import narrow_setup
import narrow_setup_index
from lib import yaml_fast
from lib.repo_paths import REPO

from _setup_role_fixtures import ALPHA, BETA, DEFAULTS, ROLE, Tree, build, narrow

# `alpha` derives a fact from the key under test; `demo_shared_mode` is what crosses files.
ALPHA_SETS_A_FACT = ALPHA + (
    "- name: Derive the shared mode\n"
    "  ansible.builtin.set_fact:\n"
    '    demo_shared_mode: "{{ demo_alpha_mode }}-shared"\n'
    "  tags: [alpha]\n"
)

ALPHA_READS_ITS_OWN_FACT = ALPHA_SETS_A_FACT + (
    "- name: Write the shared mode from alpha\n"
    "  ansible.builtin.copy:\n"
    '    content: "{{ demo_shared_mode }}"\n'
    "    dest: /etc/alpha-shared\n"
    "  tags: [alpha]\n"
)

BETA_READS_THE_FACT = BETA + (
    "- name: Write the shared mode from beta\n"
    "  ansible.builtin.copy:\n"
    '    content: "{{ demo_shared_mode }}"\n'
    "    dest: /etc/beta-shared\n"
    "  tags: [beta]\n"
)


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


def _edit_the_alpha_key(tree: Tree) -> str:
    """Change the one `defaults/` key only `alpha.yml` reads, and return the new commit."""
    tree.write(
        f"{ROLE}/defaults/main.yml",
        DEFAULTS.replace("demo_alpha_mode: fast", "demo_alpha_mode: faster"),
    )
    return tree.commit("edit the alpha mode")


# ── a fact read where it is set, against one another task file reads ────────────────────


def test_a_fact_read_only_in_the_file_that_sets_it_is_clean(tree):
    """The shape every `set_fact` in `ansible/roles/setup/` has: no widening at all."""
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_READS_ITS_OWN_FACT)
    old = tree.commit("alpha derives and reads its own fact")
    assert narrow(tree, old, _edit_the_alpha_key(tree)) == frozenset({"alpha"})


def test_a_fact_a_differently_tagged_task_file_reads_is_flagged(tree):
    """The dropped tag #2384 names: `beta` reads what `alpha` derives, so `beta` applies too."""
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_SETS_A_FACT)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA_READS_THE_FACT)
    old = tree.commit("beta reads alpha's fact")
    assert narrow(tree, old, _edit_the_alpha_key(tree)) == frozenset({"alpha", "beta"})


def test_changing_the_setter_task_file_names_the_consumers_tag(tree):
    """#2384's verify-by: a change reaching ONLY `alpha.yml` must still name `beta`.

    Editing the setter changes what the consumer reads just as surely as editing the key the
    setter derives from, and `path_tags` answers this one straight out of `tags_of`.
    """
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_SETS_A_FACT)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA_READS_THE_FACT)
    old = tree.commit("beta reads alpha's fact")
    tree.write(
        f"{ROLE}/tasks/alpha.yml", ALPHA_SETS_A_FACT.replace("-shared", "-shared-v2")
    )
    assert narrow(tree, old, tree.commit("edit the setter")) == frozenset(
        {"alpha", "beta"}
    )


def test_a_fact_only_a_template_reads_names_that_templates_readers(tree):
    """The live shape: `k3s_longhorn_b2_region` is set in a task and read in a template.

    The fact's consumer is `beta.conf.j2`, which `beta.yml` renders, so the walk has to cross
    the template edge to reach a tag rather than stopping at the task files.
    """
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_SETS_A_FACT)
    tree.write(f"{ROLE}/templates/beta.conf.j2", "shared = {{ demo_shared_mode }}\n")
    old = tree.commit("a template reads alpha's fact")
    assert narrow(tree, old, _edit_the_alpha_key(tree)) == frozenset({"alpha", "beta"})


def test_a_fact_nothing_else_reads_still_narrows_to_its_setter(tree):
    """A fact only the `set_fact` line names must not refuse the whole derivation.

    The setter's own text carries the name, so the fact always has at least one reader and
    `key_readers` never reaches its "nothing in this role reads" refusal on one.
    """
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA_SETS_A_FACT)
    old = tree.commit("alpha derives a fact nothing reads")
    assert narrow(tree, old, _edit_the_alpha_key(tree)) == frozenset({"alpha"})


def test_two_task_files_deriving_facts_from_each_other_terminate(tree):
    """A fact cycle across two task files answers with both tags instead of recursing away."""
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA_SETS_A_FACT + "- name: Derive from beta's fact\n"
        "  ansible.builtin.set_fact:\n"
        '    demo_alpha_fact: "{{ demo_beta_fact }}"\n'
        "  tags: [alpha]\n",
    )
    tree.write(
        f"{ROLE}/tasks/beta.yml",
        BETA_READS_THE_FACT + "- name: Derive from alpha's fact\n"
        "  ansible.builtin.set_fact:\n"
        '    demo_beta_fact: "{{ demo_alpha_fact }}"\n'
        "  tags: [beta]\n",
    )
    old = tree.commit("each file derives from the other's fact")
    assert narrow(tree, old, _edit_the_alpha_key(tree)) == frozenset({"alpha", "beta"})


# ── the census that keeps the module docstring's "widens nothing today" claim true ──────

# `setup/k3s` is the role the whole narrowing exists for, and these are the two facts it
# derives. A frozenset rather than a count, so a rename or a move fails by name.
K3S_FACTS = frozenset({"k3s_coredns_corefile", "k3s_longhorn_b2_region"})


def _facts_of(role_dir) -> dict[str, set[str]]:
    """Fact name -> the task files of this role that derive it, read off the working tree.

    Through `_is_role_yaml`, not a `*.yml` glob, so the census sees the same files the index
    does — a role that grows a `tasks/main.json` stays in scope (#2434).
    """
    out: dict[str, set[str]] = {}
    for path in sorted(p for p in (role_dir / "tasks").iterdir() if p.is_file()):
        if not narrow_setup_index._is_role_yaml(path.name):
            continue
        doc = yaml_fast.safe_load(path.read_text())
        for fact in narrow_setup_index._set_facts(doc if isinstance(doc, list) else []):
            out.setdefault(fact, set()).add(path.name)
    return out


def test_the_k3s_role_derives_exactly_the_facts_this_census_names():
    """Non-vacuity: the census below has a live subject in the role it was written for."""
    found = _facts_of(REPO / "ansible/roles/setup/k3s")
    assert set(found) == K3S_FACTS, sorted(found)


# The setup roles whose index refuses before any fact is reached: `common` is include-only and
# has no `tasks/main`, and no playbook lists it under `roles:`.
ROLES_WITHOUT_AN_ENTRY_POINT = frozenset({"common"})


# The fact-setting task files that reach `tags_of` without refusing, so the census below
# actually measures them. Named rather than counted: every other setter in the tree refuses
# for a reason older than this edge — untagged, or not statically imported from `tasks/main` —
# and a regression that silenced these five would leave `widened` empty and the census green.
MEASURED_SETTERS = frozenset(
    {
        "initial_setup:tasks/host-basics.yml",
        "k3s:tasks/coredns.yml",
        "k3s:tasks/longhorn-backup.yml",
        "optimize_pi:tasks/main.yml",
        "sops_setup:tasks/main.yml",
    }
)


def test_the_fact_edge_widens_no_real_setup_role_today():
    """Every fact a REACHABLE, tagged task file derives is read where the same tags apply.

    That is what `narrow_setup`'s docstring means by the edge being inert for those files, and
    it is the claim a census over task-file text alone cannot make: `key_readers` follows a
    fact named in a template to whichever task file renders THAT template, which need not be
    the setter. `k3s_longhorn_b2_region` takes exactly that path, so the comparison has to run
    through `tags_of` rather than over the sources.

    A widening is a prompt to re-read the answer, not a defect: the wider one is correct. What
    it must not do is arrive unannounced.
    """
    measured: set[str] = set()
    widened: dict[str, list[str]] = {}
    for role_dir in sorted(
        p for p in (REPO / "ansible/roles/setup").iterdir() if p.is_dir()
    ):
        if role_dir.name in ROLES_WITHOUT_AN_ENTRY_POINT:
            continue
        index = narrow_setup_index.RoleIndex(role_dir.name, "HEAD", str(REPO))
        for rel, facts in sorted(index.facts.items()):
            if not facts:
                continue
            try:
                added = index.tags_of(rel) - (index.tags[rel] or frozenset())
            except narrow_setup.CannotNarrow:
                # The file refuses for a reason older than this edge — untagged, or not
                # statically imported from the role's `tasks/main`.
                continue
            measured.add(f"{role_dir.name}:{rel}")
            if added:
                widened[f"{role_dir.name}:{rel}"] = sorted(added)
    assert MEASURED_SETTERS <= measured, sorted(MEASURED_SETTERS - measured)
    assert not widened, widened


def test_every_setup_role_this_census_skips_really_has_no_entry_point():
    """Non-vacuity for the skip list: a role that grows a `tasks/main` rejoins the census."""
    for role in sorted(ROLES_WITHOUT_AN_ENTRY_POINT):
        with pytest.raises(narrow_setup.CannotNarrow, match="holds no main file"):
            narrow_setup_index.RoleIndex(role, "HEAD", str(REPO))


def test_the_derivation_follows_a_fact_in_the_real_k3s_role():
    """The edge is wired to the real tree, not only to the demo fixture.

    `k3s_longhorn_b2_region` is read in `templates/longhorn-b2-secret.yaml.j2`, so asking the
    real role's index for the fact's readers has to come back with tags.
    """
    index = narrow_setup_index.RoleIndex("k3s", "HEAD", str(REPO))
    assert index.facts["tasks/longhorn-backup.yml"] == frozenset(
        {"k3s_longhorn_b2_region"}
    )
    assert index.key_readers("k3s_longhorn_b2_region") == frozenset(
        {"longhorn_backup", "longhorn_r2"}
    )


def test_a_set_fact_option_is_not_read_as_a_fact():
    """`cacheable:` is `set_fact`'s own option, so it is not a variable the role derives."""
    doc = narrow_setup_index.yaml_fast.safe_load(
        "---\n- name: Derive\n  ansible.builtin.set_fact:\n"
        "    demo_key: value\n    cacheable: true\n"
    )
    assert narrow_setup_index._set_facts(doc) == {"demo_key"}
