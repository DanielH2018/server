"""What `reach` answers for every caller asking which services a path list reaches.

Run: uv run pytest scripts/deploy_tools/tests/test_reach.py
"""

from reach import reach, tag_for

_SERVICE = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
_BRINGUP = "ansible/k3s-bringup.yml"
# A setup-role file that two k8s roles ship a copy of, and that no k8s role `lookup()`s.
_COPIED = "hosts/lib/host_lib.py"
# A crowdsec file that authelia and traefik `lookup()` to embed it.
_LOOKED_UP = "ansible/roles/k8s/crowdsec/files/crowdsec-whitelist.yaml"


def test_a_quiet_path_is_dropped_before_the_mapper_reads_it():
    """A playbook named for three edited comments has nothing to apply."""
    r = reach([_SERVICE, _BRINGUP], quiet=[_BRINGUP])
    assert r.manual == []
    assert r.changes.flux == {"sonarr"}
    assert r.loud == [_SERVICE]


def test_a_loud_bring_up_playbook_is_reported_as_manual():
    """The rejecting half: the bring-up playbooks run by hand and park the tick outright."""
    assert reach([_SERVICE, _BRINGUP]).manual == [_BRINGUP]


def test_a_copied_file_names_no_k8s_role_that_holds_a_copy():
    """The landing gates the promotion's changed set, so a path names only its own role."""
    r = reach([_COPIED])
    assert r.changes.flux == set()
    assert {"configarr", "janitorr"}.isdisjoint(r.touched())
    assert r.tags({"configarr", "janitorr"}) == set()


def test_tags_names_a_looked_up_files_own_role_and_not_its_readers():
    r = reach([_LOOKED_UP])
    assert r.tags({"crowdsec", "authelia", "traefik"}) == {"crowdsec"}
    assert "crowdsec" in r.touched()
    assert {"authelia", "traefik"}.isdisjoint(r.touched())


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
