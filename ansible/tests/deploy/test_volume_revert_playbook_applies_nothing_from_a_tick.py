"""A change to `ansible/volume-revert.yml` applies nothing from a GitOps tick.

The playbook is the revert runbook's entry point (docs/volume-snapshot-drills.md, *The
revert runbook*). It scales a service to zero and reverts its volumes, so it must never run
unattended. The deployer maps each changed path to a plane, and this path must map to none.
Matching no prefix is what keeps it there: a later prefix such as `ansible/` or `ansible/*.yml`
would route it to an apply, and this test goes red.

Run: uv run pytest ansible/tests/deploy/test_volume_revert_playbook_applies_nothing_from_a_tick.py
"""

from deployer_lib.deploy_changes import ChangeSet, services_from_changed_paths

from _helpers import ANSIBLE

PLAYBOOK = "ansible/volume-revert.yml"


def test_the_playbook_exists_at_the_path_checked() -> None:
    """Non-vacuity: a renamed playbook would leave the routing test checking a dead path."""
    assert (ANSIBLE / "volume-revert.yml").is_file()


def test_a_change_to_the_playbook_routes_to_no_plane() -> None:
    """An empty ChangeSet is the fast-forward with no apply, the same as a docs-only push.

    Compared with the whole empty ChangeSet, so a field a later change adds still has to stay
    empty for this path.
    """
    assert services_from_changed_paths([PLAYBOOK]) == ChangeSet()


def test_a_routed_playbook_fails_the_same_comparison() -> None:
    """The red half: `deploy.yml` routes to the deploy plane, so the comparison above can fail."""
    assert services_from_changed_paths(["ansible/deploy.yml"]) != ChangeSet()
