"""`deploy_cross_role.k8s_lookup_readers`: the k8s roles that `lookup()` another role's file.

A change to such a file must deploy its readers as well as its owner. uptime-kuma renders a
push tile for every row of monitor-bridge's `files/check_table.py` (#3781), so a new check
landed as `--tags monitor-bridge` alone would ship the check and leave its tile undeployed. The
landing (`reach.Reach.tags`), the deployer's defer alert and the secret census all read
this derivation.

Run: uv run pytest ansible/tests/deploy/test_k8s_lookup_readers.py
"""

from deploy_tools.land_lib import land_tags
from deploy_cross_role import k8s_lookup_map, k8s_lookup_readers
from lib.repo_paths import REPO

CHECK_TABLE = "ansible/roles/k8s/monitor-bridge/files/check_table.py"


def test_the_check_table_reaches_uptime_kuma_and_not_its_owner():
    # monitor-bridge's own env-secret reads the table too; the owner is not an extra reader.
    assert k8s_lookup_readers([CHECK_TABLE], REPO) == {"uptime-kuma"}


def test_crowdsec_allowlists_reach_both_roles_that_embed_them():
    path = "ansible/roles/k8s/crowdsec/files/crowdsec-whitelist.yaml"
    assert k8s_lookup_readers([path], REPO) == {"authelia", "traefik"}


def test_a_file_no_other_role_reads_reaches_nobody():
    assert (
        k8s_lookup_readers(["ansible/roles/k8s/monitor-bridge/files/registry.py"], REPO)
        == set()
    )


def test_a_reader_is_found_in_a_fixture_tree(tmp_path):
    """The derivation reads the tree it is given, so a reader written tomorrow is found."""
    k8s = tmp_path / "ansible" / "roles" / "k8s"
    (k8s / "owner" / "files").mkdir(parents=True)
    (k8s / "reader" / "templates").mkdir(parents=True)
    (k8s / "reader" / "templates" / "x.yaml.j2").write_text(
        "data: {{ lookup('file', playbook_dir + '/roles/k8s/owner/files/a.txt') }}\n"
        "# roles/k8s/owner/files/b.txt is named in prose only\n"
    )
    assert k8s_lookup_map(tmp_path) == {"roles/k8s/owner/files/a.txt": {"reader"}}
    assert (
        k8s_lookup_readers(["ansible/roles/k8s/owner/files/b.txt"], tmp_path) == set()
    )


def test_a_check_table_change_lands_with_the_tile_role():
    tags, source = land_tags.derive([CHECK_TABLE], changed_files=1)
    assert source == "pr"
    assert tags == ["monitor-bridge", "uptime-kuma"]
