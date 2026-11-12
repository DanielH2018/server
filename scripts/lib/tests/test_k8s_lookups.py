"""`k8s_lookups.k8s_lookup_map`: the k8s roles that `lookup()` another role's file.

authelia and traefik embed crowdsec's allowlists. The secret census reads this map.

Run: uv run pytest scripts/lib/tests/test_k8s_lookups.py
"""

from lib.k8s_lookups import k8s_lookup_map
from lib.repo_paths import REPO

CHECK_TABLE = "ansible/roles/k8s/monitor-bridge/files/check_table.py"


def test_the_map_names_the_live_readers_and_no_unread_file():
    lookups = k8s_lookup_map(REPO)
    assert lookups["roles/k8s/crowdsec/files/crowdsec-whitelist.yaml"] == {
        "authelia",
        "traefik",
    }
    # uptime-kuma looked the table up until M6 batch A6's Cutover-2 deleted its bridge tiles.
    assert CHECK_TABLE.removeprefix("ansible/") not in lookups


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


def test_a_lookup_with_keyword_arguments_is_a_reader(tmp_path):
    """A keyword argument after the path still names the file."""
    reader = tmp_path / "ansible" / "roles" / "k8s" / "reader" / "templates"
    reader.mkdir(parents=True)
    (reader / "x.yaml.j2").write_text(
        "data: {{ lookup('file', playbook_dir + '/roles/k8s/flux/defaults/main.yml',"
        " rstrip=False) }}\n"
    )
    assert k8s_lookup_map(tmp_path) == {"roles/k8s/flux/defaults/main.yml": {"reader"}}
