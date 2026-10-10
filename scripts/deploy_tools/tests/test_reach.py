"""What `reach` answers for every caller asking which services a path list reaches.

Run: uv run pytest scripts/deploy_tools/tests/test_reach.py
"""

from reach import reach, tag_for

_SERVICE = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
_BRINGUP = "ansible/k3s-bringup.yml"
# A setup-role file that two k8s roles ship a copy of, and that no k8s role `lookup()`s.
_COPIED = "ansible/roles/setup/common/files/host_lib.py"
# A monitor-bridge file uptime-kuma `lookup()`s to render one tile per check (#3781).
_LOOKED_UP = "ansible/roles/k8s/monitor-bridge/files/check_table.py"


def test_a_quiet_path_is_dropped_before_the_mapper_reads_it():
    """A playbook named for three edited comments has nothing to apply."""
    r = reach([_SERVICE, _BRINGUP], quiet=[_BRINGUP])
    assert r.manual == []
    assert r.changes.k8s == {"sonarr"}
    assert r.loud == [_SERVICE]


def test_a_loud_bring_up_playbook_is_reported_as_manual():
    """The rejecting half: the bring-up playbooks run by hand and park the tick outright."""
    assert reach([_SERVICE, _BRINGUP]).manual == [_BRINGUP]


def test_changes_and_tags_both_expand_a_copied_file():
    """A landing deploys the k8s roles shipping a copy, as `deploy.sh --changed` does (#4136)."""
    r = reach([_COPIED])
    declared = {"configarr", "janitorr"}
    assert {"configarr", "janitorr"} <= r.changes.k8s
    assert r.tags(declared) == {"configarr", "janitorr"}
    assert r.tags({"configarr"}) == {"configarr"}
    assert {"configarr", "janitorr"} <= r.touched()


def test_tags_adds_a_lookup_reader_and_changes_does_not():
    r = reach([_LOOKED_UP])
    declared = {"monitor-bridge", "uptime-kuma"}
    assert r.tags(declared) == {"monitor-bridge", "uptime-kuma"}
    assert "uptime-kuma" not in r.changes.k8s | r.changes.services
    assert "uptime-kuma" in r.touched()


def test_tags_keeps_only_declared_roles_and_drops_role_tests():
    r = reach([_SERVICE, "ansible/roles/k8s/sonarr/tests/test_sonarr.py"])
    assert r.tags({"sonarr"}) == {"sonarr"}
    assert r.tags(set()) == set()
    assert (
        reach(["ansible/roles/k8s/sonarr/tests/test_sonarr.py"]).tags({"sonarr"})
        == set()
    )


def test_tag_for_rejects_a_path_outside_the_role_trees():
    assert tag_for("ansible/inventory/host_vars/daniel-box.yml", {"daniel-box"}) is None


def test_deploy_plane_is_the_deployers_split_not_a_prefix_match():
    """A test file under a deploy-plane prefix reaches no host, as the tick reads it."""
    play = "ansible/tasks/k8s_batch.yml"
    r = reach([_SERVICE, play, "ansible/tasks/tests/test_k8s_batch.py", _BRINGUP])
    assert r.deploy_plane == [play]
