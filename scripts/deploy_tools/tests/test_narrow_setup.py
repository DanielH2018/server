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

import deploy_narrow
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
- name: Release the beta cron scripts
  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/release_bin.yml"
  vars:
    release_bin_templates: "{{ demo_release_groups | map(attribute='templates') | flatten }}"
  tags: [beta]
"""

DEFAULTS = """\
---
demo_alpha_mode: fast
demo_beta_mode: slow
demo_orphan_key: nobody-reads-this
# A host script's template named in a data structure rather than in a task's `src:` — the
# shape `setup/k3s` uses for its cron scripts, through `k3s_render_stamp_groups`.
demo_release_groups:
  - name: demo-beta
    templates:
      - beta-cron.sh.j2
"""


# The playbook the remediation prints for `demo`. The derivation refuses a role no play in it
# lists, since none of the role's own tags reach a host through it.
PLAYBOOK = "ansible/demo.yml"
PLAYBOOK_TEXT = """\
---
- name: Apply the demo role
  hosts: localhost
  roles:
    - { role: demo, tags: ["demo"] }
"""


def build(tmp_path) -> Tree:
    """A checkout holding one setup role with two tagged task files and one untagged one."""
    tree = Tree(tmp_path / "repo")
    tree.write(PLAYBOOK, PLAYBOOK_TEXT)
    tree.write(f"{ROLE}/tasks/main.yml", MAIN)
    tree.write(f"{ROLE}/tasks/alpha.yml", ALPHA)
    tree.write(f"{ROLE}/tasks/beta.yml", BETA)
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "mode = {{ demo_beta_mode }}\n")
    tree.write(f"{ROLE}/templates/beta-cron.sh.j2", "#!/bin/sh\necho beta\n")
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
    return narrow_setup.role_tags("demo", "demo", old, new, str(tree.root), PLAYBOOK)


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


def test_a_template_named_only_in_a_defaults_structure_maps_to_that_keys_readers(tree):
    """The shape a host script takes: the name is in `defaults/`, not in any task's `src:`.

    The task file naming the KEY is the one that renders the template, so its tags are the
    answer — wider than the one import site, still far narrower than the whole role.
    """
    tree.write(f"{ROLE}/templates/beta-cron.sh.j2", "#!/bin/sh\necho beta beta\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"beta"})


def test_a_role_claude_md_does_not_block_the_narrowing(tree):
    """Prose reaches no host, so it adds no tag requirement.

    Measured against the k3s role's history: 4 of the 5 most recent ranges touching it carry
    the role's own `CLAUDE.md`, so refusing on one would leave this almost never firing.
    """
    tree.write(f"{ROLE}/CLAUDE.md", "# Demo\n")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha"})


def test_a_range_of_nothing_but_prose_is_flagged(tree):
    """The rejecting half: skipping is not the same as narrowing to an empty `--tags`.

    An empty `--tags` value runs the whole playbook, so a range the deployer should not have
    deferred at all must refuse rather than answer nothing.
    """
    tree.write(f"{ROLE}/CLAUDE.md", "# Demo\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="reaches no host"):
        narrow(tree, *_refs(tree))


# ── a derived tag must be reachable in the playbook the remediation prints ─────────────

GAMMA = """\
---
- name: A topic another playbook imports on its own
  ansible.builtin.debug:
    msg: gamma
  tags: [gamma]
"""


def test_a_task_file_main_yml_imports_inside_a_block_is_clean(tree):
    """A static import nested in a block still runs under the role's entry."""
    tree.write(
        f"{ROLE}/tasks/main.yml",
        MAIN
        + "- name: A grouped topic\n  block:\n"
        + "    - name: The third topic\n      ansible.builtin.import_tasks: gamma.yml\n",
    )
    tree.write(f"{ROLE}/tasks/gamma.yml", GAMMA)
    old = tree.commit("import gamma")
    tree.write(f"{ROLE}/tasks/gamma.yml", GAMMA + "# touched\n")
    assert narrow(tree, old, tree.commit("touch gamma")) == frozenset({"gamma"})


def test_a_task_file_main_yml_never_imports_is_flagged(tree):
    """`tasks/storage_smoke.yml` is imported by `k3s-storage-smoke.yml`, not by `main.yml`.

    `k3s-bringup.yml --tags storage_smoke` therefore selects only `always` tasks and exits 0.
    """
    tree.write(f"{ROLE}/tasks/gamma.yml", GAMMA)
    with pytest.raises(narrow_setup.CannotNarrow, match="not statically imported"):
        narrow(tree, *_refs(tree))


def test_a_task_file_reached_only_by_include_tasks_is_flagged(tree):
    """A dynamic include runs only when the include task itself is selected."""
    tree.write(
        f"{ROLE}/tasks/main.yml",
        MAIN + "- name: Dynamic\n  ansible.builtin.include_tasks: gamma.yml\n",
    )
    tree.write(f"{ROLE}/tasks/gamma.yml", GAMMA)
    old = tree.commit("include gamma")
    tree.write(f"{ROLE}/tasks/gamma.yml", GAMMA + "# touched\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="not statically imported"):
        narrow(tree, old, tree.commit("touch gamma"))


def test_a_role_the_printed_playbook_does_not_list_is_flagged(tree):
    tree.write(PLAYBOOK, PLAYBOOK_TEXT.replace("role: demo", "role: other"))
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="lists demo under roles"):
        narrow(tree, *_refs(tree))


def test_the_real_k3s_roles_reachable_task_files_are_named_as_such():
    """Named members both ways, so a walk that went empty or all-inclusive fails by name.

    `storage_smoke.yml` belongs to `k3s-storage-smoke.yml`, and `agent.yml`/`agent_verify.yml`
    to the `k3s_agent` plays whose hosts are empty without `-e join_agent=...`.
    """
    reachable = narrow_setup.RoleIndex("k3s", "HEAD", str(REPO)).reachable
    assert {
        "tasks/server.yml",
        "tasks/kubeconfig.yml",
        "tasks/coredns.yml",
    } <= reachable
    assert not reachable & {
        "tasks/storage_smoke.yml",
        "tasks/agent.yml",
        "tasks/agent_verify.yml",
    }


# ── the cheap refusals: a template cycle, a rendered `.md`, a binary file ──────────────


def test_a_template_cycle_reaching_no_task_file_is_flagged(tree):
    """Two templates naming only each other answer an empty set, which is not "nothing"."""
    tree.write(f"{ROLE}/templates/a.j2", "{% include 'b.j2' %}\n")
    tree.write(f"{ROLE}/templates/b.j2", "{% include 'a.j2' %}\n")
    old = tree.commit("cycle")
    tree.write(f"{ROLE}/templates/a.j2", "{% include 'b.j2' %} x\n")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    with pytest.raises(
        narrow_setup.CannotNarrow, match="only templates naming each other"
    ):
        narrow(tree, old, tree.commit("touch a"))


def test_a_markdown_file_a_task_renders_narrows_like_any_template(tree):
    """A `.md` under `templates/` reaches a host; skipping it as prose dropped it."""
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA
        + "- name: Render the alpha notes\n  ansible.builtin.template:\n"
        + "    src: alpha-notes.md\n    dest: /etc/alpha.md\n  tags: [alpha]\n",
    )
    tree.write(f"{ROLE}/templates/alpha-notes.md", "# notes\n")
    old = tree.commit("notes")
    tree.write(f"{ROLE}/templates/alpha-notes.md", "# notes, edited\n")
    assert narrow(tree, old, tree.commit("edit notes")) == frozenset({"alpha"})


def test_a_binary_file_under_files_does_not_block_the_narrowing(tree):
    """`files/` is matched by name only, so its bytes are never decoded."""
    (tree.root / ROLE / "files").mkdir(parents=True)
    (tree.root / ROLE / "files" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    old = tree.commit("blob")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    assert narrow(tree, old, tree.commit("touch alpha")) == frozenset({"alpha"})


def test_a_binary_template_is_flagged_rather_than_raised(tree):
    (tree.root / ROLE / "templates" / "blob.j2").write_bytes(b"\xff\xfe\x00")
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="is not text"):
        narrow(tree, *_refs(tree))


# ── the deployer's real argv reaches this module's real CLI ────────────────────────────


def test_the_deployers_argv_is_one_narrow_setup_main_accepts(tree, capsys):
    """The tick fakes replace the subprocess, so only this sees a flag the CLI does not take."""

    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    old, new = _refs(tree)
    argv = deploy_narrow.narrow_setup_argv("demo", "demo", PLAYBOOK, old, new)
    assert argv[4] == deploy_narrow.NARROW_SETUP_SCRIPT
    assert narrow_setup.main([*argv[5:], "--repo", str(tree.root)]) == 0
    assert capsys.readouterr().out.strip() == "alpha"


def test_the_deployers_argv_for_an_unlisted_role_is_refused(tree, capsys):

    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    old, new = _refs(tree)
    argv = deploy_narrow.narrow_setup_argv(
        "demo", "demo", "ansible/absent.yml", old, new
    )
    assert narrow_setup.main([*argv[5:], "--repo", str(tree.root)]) == 1
    assert capsys.readouterr().out == ""
