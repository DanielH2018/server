"""The owed-host note reads a stamped file's record on a host before naming it (#7398).

The note and the task walk are the real ones, over this checkout's roles. Only the host read is
fake. Each test also pins `daniel-server` in the note it expects to stay, so an empty note
cannot pass one that expects the host to stay owed.
"""

import hashlib

from deploy_tools.land_lib.land_reach import remaining_setup_hosts_note
from diagnostics.probe_lib import core
from lib.repo_paths import REPO

WITNESS = "ansible/roles/setup/initial_setup/files/loki_route_health.py"
WITNESS_LIVE = "/opt/loki-route-health/loki_route_health.py"
# The second path PR #7295 changed. No setup role ships it, so it owes nothing either way.
COMPONENT_COPY = "hosts/components/loki-route-witness/loki_route_health.py"
CRONS = "ansible/roles/setup/initial_setup/tasks/crons.yml"
DRIFT_LIB = "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
WITNESS_SHA = hashlib.sha256((REPO / WITNESS).read_bytes()).hexdigest()
TEMPLATE = "ansible/roles/setup/initial_setup/templates/loki-read-route-health.sh.j2"
TEMPLATE_SHA = hashlib.sha256((REPO / TEMPLATE).read_bytes()).hexdigest()
FRAGMENT = "/var/lib/homelab/setup-render-manifest.d/loki-route-witness"


def _reader(row, reads=None):
    def read_host(plan, host):
        if reads is not None:
            reads.append((plan, host))
        return {"stat": {WITNESS_LIVE: row}, "files": {}}

    return read_host


def _fragment_reader(row):
    return lambda plan, host: {"stat": {}, "files": {FRAGMENT: row}}


def _note(files, read_host):
    return remaining_setup_hosts_note(
        files, "daniel-box", ref="HEAD", repo=REPO, read_host=read_host
    )


def test_a_host_holding_the_merge_commits_bytes_is_owed_nothing():
    row = {"status": "ok", "sha256": WITNESS_SHA}
    assert _note([WITNESS, COMPONENT_COPY], _reader(row)) == ""


def test_a_copy_only_rootq_could_compare_counts_as_held():
    row = {"status": "ok", "sha256": None, "via": "rootq", "match": True}
    assert _note([WITNESS], _reader(row)) == ""


def test_a_copy_with_other_bytes_keeps_the_host_owed():
    row = {"status": "ok", "sha256": "0" * 64}
    assert "`initial_setup` also reaches daniel-server" in _note(
        [WITNESS], _reader(row)
    )


def test_a_missing_copy_keeps_the_host_owed():
    assert "daniel-server" in _note([WITNESS], _reader({"status": "absent"}))


def test_a_host_that_cannot_be_read_stays_owed(capsys):
    def read_host(plan, host):
        raise core.Unreadable(f"could not read {host}: exit 255")

    assert "daniel-server" in _note([WITNESS], read_host)
    assert "could not read what daniel-server holds" in capsys.readouterr().err


def test_a_changed_sibling_with_no_stamped_copy_keeps_the_host_owed():
    row = {"status": "ok", "sha256": WITNESS_SHA}
    assert "daniel-server" in _note([WITNESS, CRONS], _reader(row))


def test_the_read_asks_for_the_merge_commits_hash():
    reads = []
    _note([WITNESS, COMPONENT_COPY], _reader({"status": "absent"}, reads))
    assert reads == [
        (
            {
                "stat": [WITNESS_LIVE],
                "rootq": {WITNESS_LIVE: WITNESS_SHA},
                "files": [],
            },
            "daniel-server",
        )
    ]


def test_two_stamped_files_read_each_host_once():
    """`ufw limit ssh` refuses a sixth connection in 30 seconds."""
    reads = []
    _note([WITNESS, DRIFT_LIB], _reader({"status": "absent"}, reads))
    hosts = [host for _, host in reads]
    assert "daniel-server" in hosts
    assert len(hosts) == len(set(hosts))


def test_a_template_the_host_last_rendered_at_this_source_is_owed_nothing():
    row = {"status": "ok", "text": f"{TEMPLATE} {TEMPLATE_SHA}\nother.j2 {'1' * 64}\n"}
    assert _note([TEMPLATE], _fragment_reader(row)) == ""


def test_a_template_rendered_from_another_source_keeps_the_host_owed():
    row = {"status": "ok", "text": f"{TEMPLATE} {'0' * 64}\n"}
    assert "daniel-server" in _note([TEMPLATE], _fragment_reader(row))


def test_a_missing_render_fragment_keeps_the_host_owed():
    assert "daniel-server" in _note([TEMPLATE], _fragment_reader({"status": "absent"}))
