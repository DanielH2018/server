"""The two questions `changes_for` answers for all four of its callers.

Run: uv run pytest scripts/deploy_tools/tests/test_land_changes.py
"""

from land_changes import changes_for

_SERVICE = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
_BRINGUP = "ansible/k3s-bringup.yml"


def test_a_quiet_path_is_dropped_before_the_mapper_reads_it():
    """A playbook named for three edited comments has nothing to apply (#848)."""
    loud = changes_for([_SERVICE, _BRINGUP], quiet=[_BRINGUP])
    assert loud.manual == []
    assert loud.changes.k8s == {"sonarr"}


def test_a_loud_bring_up_playbook_is_reported_as_manual():
    """The rejecting half: the bring-up playbooks run by hand and park the tick outright."""
    assert changes_for([_SERVICE, _BRINGUP]).manual == [_BRINGUP]
