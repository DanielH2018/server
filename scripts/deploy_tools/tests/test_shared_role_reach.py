"""Which shared-role changes `land.sh` stops asking a hand to apply, and which it still does.

Every rule in `shared_role_reach.py` is a PAIR: one change that reaches no rendered manifest
(so the role drops out of `plane_note`) and one that does (so it stays). A predicate that
answered True for everything and one that answered True for nothing read identically from the
settling side alone.

The fixture is a throwaway checkout with a shared role (`manifests`, no `containers_list`
entry) and one service that has one, shaped like `_narrow_fixtures.build_tree`.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_reach.py
"""

import pytest

import shared_role_reach
from deploy_tools import land_tags

from _narrow_fixtures import Tree, _refs

DECLARED = {"sonarr"}

DEFAULTS = """\
manifests_release_dir: /var/lib/homelab/k8s-releases.d
manifests_rollout_timeout_default: 600s
manifests_prune: false
"""

TASKS = """\
- name: Wait for the rollout
  ansible.builtin.command: >-
    kubectl rollout status
    --timeout={{ manifests_rollout_timeout_default }}
"""


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = Tree(tmp_path / "repo")
    t.write("ansible/roles/k8s/manifests/defaults/main.yml", DEFAULTS)
    t.write("ansible/roles/k8s/manifests/tasks/main.yml", TASKS)
    t.write(
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2", "image: sonarr:1\n"
    )
    t.commit("base")
    return t


def _only(tree: Tree, *files: str) -> bool:
    old, new = _refs(tree)
    return shared_role_reach.deploy_time_only(
        "manifests", list(files), f"{old}..{new}", tree.root
    )


def test_a_default_only_a_task_reads_is_deploy_time_only(tree: Tree):
    """CLEAN half: PR #2460's change, which no live object carries (#2462)."""
    tree.write(
        "ansible/roles/k8s/manifests/defaults/main.yml",
        DEFAULTS.replace("600s", "900s"),
    )
    tree.write(
        "ansible/roles/k8s/manifests/tasks/main.yml",
        TASKS.replace(
            "{{ manifests_rollout_timeout_default }}",
            "{{ manifests_rollout_timeout | default(manifests_rollout_timeout_default) }}",
        ),
    )
    assert _only(
        tree,
        "ansible/roles/k8s/manifests/defaults/main.yml",
        "ansible/roles/k8s/manifests/tasks/main.yml",
    )


def test_a_task_line_that_is_not_the_changed_keys_consumer_needs_a_hand(tree: Tree):
    """FLAGGED half: a `tasks/` edit doing anything else can move the bytes it renders."""
    tree.write(
        "ansible/roles/k8s/manifests/defaults/main.yml",
        DEFAULTS.replace("600s", "900s"),
    )
    tree.write(
        "ansible/roles/k8s/manifests/tasks/main.yml",
        TASKS.replace("rollout status", "rollout status --watch"),
    )
    assert not _only(
        tree,
        "ansible/roles/k8s/manifests/defaults/main.yml",
        "ansible/roles/k8s/manifests/tasks/main.yml",
    )


def test_a_default_a_template_mentions_still_needs_a_hand(tree: Tree):
    """FLAGGED half: a key a template reads can render into a live object."""
    tree.write(
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "image: sonarr:1\nprune: {{ manifests_prune }}\n",
    )
    tree.commit("a template reads the key")
    tree.write(
        "ansible/roles/k8s/manifests/defaults/main.yml",
        DEFAULTS.replace("manifests_prune: false", "manifests_prune: true"),
    )
    assert not _only(tree, "ansible/roles/k8s/manifests/defaults/main.yml")


def test_a_comment_only_task_change_is_deploy_time_only(tree: Tree):
    """CLEAN half: PR #2575 added a `# DECIDED:` comment and was sent to deploy.yml (#2581)."""
    tree.write(
        "ansible/roles/k8s/manifests/tasks/main.yml",
        "# DECIDED: the timeout is a deploy-time read.\n" + TASKS,
    )
    assert _only(tree, "ansible/roles/k8s/manifests/tasks/main.yml")


def test_a_hash_line_inside_a_block_scalar_still_needs_a_hand(tree: Tree):
    """FLAGGED half: inside a `shell: |` a `#` line is script content, not a comment."""
    script = """\
- name: Stamp the release
  ansible.builtin.shell: |
    # stamp
    echo old > /var/lib/homelab/stamp
"""
    tree.write("ansible/roles/k8s/manifests/tasks/main.yml", script)
    tree.commit("a task carrying a script")
    tree.write(
        "ansible/roles/k8s/manifests/tasks/main.yml",
        script.replace("# stamp", "# stamp the release record"),
    )
    assert not _only(tree, "ansible/roles/k8s/manifests/tasks/main.yml")


def test_a_shared_role_template_change_always_needs_a_hand(tree: Tree):
    """FLAGGED half: those bytes are applied, whatever any key scan says."""
    tree.write("ansible/roles/k8s/manifests/templates/pvc.yaml.j2", "kind: PVC\n")
    assert not _only(tree, "ansible/roles/k8s/manifests/templates/pvc.yaml.j2")


def test_an_unreadable_range_keeps_the_role_loud(tree: Tree):
    """Doubt keeps the note: `pr_range` is empty whenever the PR's own refs could not be read."""
    tree.write(
        "ansible/roles/k8s/manifests/defaults/main.yml",
        DEFAULTS.replace("600s", "900s"),
    )
    tree.commit("change")
    assert not shared_role_reach.deploy_time_only(
        "manifests", ["ansible/roles/k8s/manifests/defaults/main.yml"], "", tree.root
    )


def test_the_note_drops_a_deploy_time_only_shared_role(tree: Tree):
    """The wiring: `plane_note` stops asking for a full deploy.yml for such a role."""
    files = [
        "ansible/roles/k8s/manifests/defaults/main.yml",
        "ansible/roles/k8s/manifests/tasks/main.yml",
    ]
    assert "manifests" in land_tags.plane_note(files, DECLARED)
    tree.write(
        "ansible/roles/k8s/manifests/defaults/main.yml",
        DEFAULTS.replace("600s", "900s"),
    )
    tree.write(
        "ansible/roles/k8s/manifests/tasks/main.yml",
        TASKS.replace(
            "{{ manifests_rollout_timeout_default }}",
            "{{ manifests_rollout_timeout | default(manifests_rollout_timeout_default) }}",
        ),
    )
    old, new = _refs(tree)
    kept = shared_role_reach.paths_a_hand_must_apply(
        files, f"{old}..{new}", tree.root, DECLARED
    )
    assert kept == []
    assert land_tags.plane_note(kept, DECLARED) == ""
