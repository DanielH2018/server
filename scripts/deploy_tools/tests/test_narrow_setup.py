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

from _narrow_fixtures import _refs
from _setup_role_fixtures import (
    ALPHA,
    BETA,
    DEFAULTS,
    MAIN,
    PLAYBOOK,
    PLAYBOOK_TEXT,
    ROLE,
    Tree,
    build,
    narrow,
)


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


# ── a template maps to the tags of the task file that renders it ────────────────────────


def test_a_template_change_narrows_to_its_own_task_files_tag(tree):
    tree.write(
        f"{ROLE}/templates/alpha.conf.j2", "mode = {{ demo_alpha_mode }} # more\n"
    )
    assert narrow(tree, *_refs(tree)) == frozenset({"alpha"})


def test_a_template_no_task_file_names_is_flagged(tree):
    tree.write(f"{ROLE}/templates/orphan.conf.j2", "nothing renders this\n")
    with pytest.raises(narrow_setup.CannotNarrow, match=r"names orphan\.conf\.j2"):
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


# ── another role's file this role ships by path maps to the task shipping it (#3306) ────

SHIPPED = "ansible/roles/setup/owner/files/shared.py"
SHIP_TASK = """\
- name: Install the owner's shared module
  ansible.builtin.copy:
    src: "{{ playbook_dir }}/roles/setup/owner/files/shared.py"
    dest: /opt/demo/shared.py
  tags: [alpha]
"""


@pytest.fixture
def ships(tree, monkeypatch) -> Tree:
    """The demo role declared a consumer of `SHIPPED`, which the owner role holds."""
    monkeypatch.setitem(
        narrow_setup.SETUP_FILES_SHIPPED_BY_OTHER_ROLES, SHIPPED, frozenset({"demo"})
    )
    tree.write(SHIPPED, "VALUE = 1\n")
    tree.commit("the owner role's module")
    return tree


def test_a_shipped_file_another_role_owns_narrows_to_the_shipping_task(ships):
    ships.write(f"{ROLE}/tasks/alpha.yml", ALPHA + SHIP_TASK)
    ships.commit("demo ships it")
    ships.write(SHIPPED, "VALUE = 2\n")
    assert narrow(ships, *_refs(ships)) == frozenset({"alpha"})


@pytest.mark.parametrize(
    ("role", "tag"),
    [("deploy_ui", "deploy-ui-code"), ("renovate_agent", "renovate-agent-code")],
)
def test_the_real_consumers_of_gitops_markers_narrow_to_their_code_tag(role, tag):
    """The two roles #3275 points at the deployer's file, read from the real tree."""
    index = narrow_setup.RoleIndex(role, "HEAD", str(REPO))
    assert index.readers_of("gitops_markers.py") == frozenset({tag})


@pytest.mark.parametrize(
    ("role", "tags"),
    [
        ("gitops_deploy", {"gitops-deploy-code"}),
        ("renovate_notify", {"renovate-notify-code"}),
    ],
)
def test_a_common_task_file_narrows_to_the_tags_importing_it(role, tags):
    """A `common/tasks/` file resolves through its importing task, as a `files/` basename
    resolves through its copy task (#3317), read from the real tree."""
    index = narrow_setup.RoleIndex(role, "HEAD", str(REPO))
    assert index.readers_of("install_host_lib.yml") == frozenset(tags)


def test_a_shipped_file_no_task_of_the_role_names_is_flagged(ships):
    ships.write(SHIPPED, "VALUE = 2\n")
    with pytest.raises(narrow_setup.CannotNarrow, match=r"names shared\.py"):
        narrow(ships, *_refs(ships))


# ── a filter plugin the role calls takes the whole role (#3874) ────────────────────────

PLUGIN = "ansible/filter_plugins/demo_filters.py"


@pytest.fixture
def calls_plugin(tree, monkeypatch) -> Tree:
    """The demo role declared a caller of `PLUGIN`'s filters."""
    monkeypatch.setitem(
        narrow_setup.SETUP_ROLES_CALLING_FILTER_PLUGINS, PLUGIN, frozenset({"demo"})
    )
    tree.write(PLUGIN, "VALUE = 1\n")
    tree.commit("the plugin")
    return tree


def test_a_filter_plugin_the_role_calls_is_flagged(calls_plugin):
    # The role's own template changes too, so a narrowing that ignored the plugin would
    # still return `alpha` and drop the plugin's reach.
    calls_plugin.write(f"{ROLE}/templates/alpha.conf.j2", "mode = 2\n")
    calls_plugin.write(PLUGIN, "VALUE = 2\n")
    with pytest.raises(narrow_setup.CannotNarrow, match=r"calls a filter from"):
        narrow(calls_plugin, *_refs(calls_plugin))


def test_a_role_change_beside_an_unchanged_plugin_still_narrows(calls_plugin):
    calls_plugin.write(f"{ROLE}/templates/alpha.conf.j2", "mode = 2\n")
    assert narrow(calls_plugin, *_refs(calls_plugin)) == frozenset({"alpha"})


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


# The setup task files #3134, #3135 and #3154 made narrowable, each with the answer `tags_of`
# gives. Before them, `crons.yml` refused on its `always` preamble, three roles had one untagged
# `main.yml`, and `gitops_deploy` and `hypervisor` reached their task files only through
# `include_tasks`, so every change to them applied the whole role.
NARROWED_SETUP_FILES = {
    ("deploy_ui", "tasks/code.yml"): {"deploy-ui-code"},
    ("deploy_ui", "tasks/service.yml"): {"deploy-ui-service"},
    ("renovate_agent", "tasks/code.yml"): {"renovate-agent-code"},
    ("renovate_agent", "tasks/service.yml"): {"renovate-agent-service"},
    ("renovate_notify", "tasks/code.yml"): {"renovate-notify-code"},
    ("renovate_notify", "tasks/service.yml"): {"renovate-notify-service"},
    ("gitops_deploy", "tasks/code.yml"): {"gitops-deploy-code"},
    ("gitops_deploy", "tasks/service.yml"): {"gitops-deploy-service"},
    ("gitops_deploy", "tasks/github_checks.yml"): {
        "cron",
        "gitops-deploy-github-checks",
    },
    ("gitops_deploy", "tasks/teardown.yml"): {"gitops-deploy-teardown"},
    ("hypervisor", "tasks/network.yml"): {"hypervisor-network"},
    ("hypervisor", "tasks/etcd_drill.yml"): {"hypervisor-etcd-drill"},
    ("hypervisor", "tasks/teardown.yml"): {"hypervisor-teardown"},
}


@pytest.mark.parametrize(("role", "rel"), sorted(NARROWED_SETUP_FILES))
def test_the_real_split_setup_roles_narrow_below_their_role_tag(role, rel):
    """Each file answers its own subject tag, and no other role in the playbook declares it."""
    index = narrow_setup.RoleIndex(role, "HEAD", str(REPO))
    tags = index.tags_of(rel)
    assert tags == NARROWED_SETUP_FILES[(role, rel)]
    playbook = (REPO / "ansible/initial_setup.yml").read_text()
    assert not tags & narrow_setup.foreign_tags(role, playbook, "HEAD", str(REPO))


def test_the_real_initial_setup_crons_file_narrows_past_its_always_preamble():
    """`crons.yml` keeps its two `always` tasks and still answers its subject tags (#3134)."""
    index = narrow_setup.RoleIndex("initial_setup", "HEAD", str(REPO))
    crons_tags = index.tags["tasks/crons.yml"]
    assert crons_tags is not None and "always" in crons_tags
    tags = index.tags_of("tasks/crons.yml")
    assert "crons" in tags
    assert not tags & {"always", "initial_setup"}


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


def test_the_real_playbooks_list_the_roles_they_apply():
    """The playbook lookup finds its subject by pattern, so it names real members.

    A playbook reshaped to `import_role` would make every narrowing refuse, silently. Both
    entry shapes are covered: the inline `{ role: k3s }` and `nut_host`'s multi-line
    `- role:` form.
    """
    for playbook, role in (
        ("ansible/k3s-bringup.yml", "k3s"),
        ("ansible/initial_setup.yml", "nut_host"),
        ("ansible/initial_setup.yml", "gitops_deploy"),
    ):
        text = (REPO / playbook).read_text()
        assert narrow_setup.playbook_applies_role(text, role), f"{playbook}: {role}"
    text = (REPO / "ansible/initial_setup.yml").read_text()
    assert not narrow_setup.playbook_applies_role(text, "k3s")


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


def test_a_markdown_file_under_templates_is_prose_and_reaches_no_tag(tree):
    """A `.md` is prose wherever it sits, which is the one answer #2810 decided on.

    This used to narrow to `alpha`, because the planted task renders the file onto a host. No
    role in the repo ships a `.md`, and
    the `no-role-ships-a-markdown-file` row of `ansible/tests/repo/test_census_rows_roles.py`
    is what holds that — so the
    carve-out only cost the deploy-plane tick a full play for a README nobody deploys. A range
    of nothing but prose reaches no host, which is the refusal below rather than a tag; the
    tick never gets here for one, because `deploy_logic.is_doc` drops it before the plane
    branches.
    """
    tree.write(
        f"{ROLE}/tasks/alpha.yml",
        ALPHA
        + "- name: Render the alpha notes\n  ansible.builtin.template:\n"
        + "    src: alpha-notes.md\n    dest: /etc/alpha.md\n  tags: [alpha]\n",
    )
    tree.write(f"{ROLE}/templates/alpha-notes.md", "# notes\n")
    old = tree.commit("notes")
    tree.write(f"{ROLE}/templates/alpha-notes.md", "# notes, edited\n")
    with pytest.raises(narrow_setup.CannotNarrow, match="reaches no host"):
        narrow(tree, old, tree.commit("edit notes"))


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


# DECIDED: these two drive `narrow_setup.main` with the argv `deploy_narrow.narrow_setup_argv`
# builds, and that function is the capability — there is no earlier signature to separate the
# defect from (#2363). The defect they name is a flag mismatch across the subprocess boundary,
# and they catch it: spelling `--role-tag` as `--role-tags` in the builder fails both with
# argparse's `unrecognized arguments`, measured 2026-09-24.
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
