"""`setup_reach_hosts.py` prints the hosts a range's setup-role change reaches beyond the tick's.

Both cases read real commits, so the derivation they run is the one the tick runs. CI clones
with full history.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_reach_hosts.py
"""

import json

import setup_reach_hosts
from deploy_tools.land_lib import land_reach
from deploy_tools.land_lib.key_readers import KeyReader

# Adds a guest rehearsal mode to the etcd drill, under `roles/setup/hypervisor/` alone.
_HYPERVISOR = "ee529af466"
# PR #6952's merge, which moved `host_python_version` in `group_vars/all.yml`.
_PYTHON_PIN = "9c8edb67a5"
# Installs `host_python_version` with no host gate, so a moved key reaches every host.
_PIN_READER = "ansible/roles/setup/initial_setup/tasks/host-basics.yml"


def _reach(capsys, old: str, new: str) -> dict:
    assert setup_reach_hosts.main([old, new, "--host", "daniel-box"]) == 0
    return json.loads(capsys.readouterr().out)


def test_a_hypervisor_range_names_daniel_server(capsys):
    reach = _reach(capsys, f"{_HYPERVISOR}~1", _HYPERVISOR)
    assert "daniel-server" in reach["hypervisor"]
    assert "daniel-box" not in reach["hypervisor"]


def test_an_empty_range_names_no_role(capsys):
    """The rejecting half: a range with no change reaches no host."""
    assert _reach(capsys, _HYPERVISOR, _HYPERVISOR) == {}


def test_the_drill_script_commit_names_daniel_server(capsys):
    """49c90e3be changed the script hypervisor copies, and no file under its role (#6411)."""
    reach = _reach(capsys, "49c90e3be~1", "49c90e3be")
    assert "daniel-server" in reach["hypervisor"]


def test_a_moved_group_vars_key_names_every_host_its_reader_runs_on(capsys):
    """#6968: the range's changed paths reached daniel-server only; the key reaches daniel-pi."""
    reach = _reach(capsys, f"{_PYTHON_PIN}~1", _PYTHON_PIN)
    assert reach["initial_setup"] == ["daniel-pi", "daniel-server"]


def test_a_host_vars_key_reaches_its_own_host_alone():
    reader = KeyReader(_PIN_READER, frozenset(), "daniel-server")
    assert land_reach.remaining_setup_hosts([], "daniel-box", key_readers=[reader]) == {
        "initial_setup": frozenset({"daniel-server"})
    }


def test_a_tick_host_vars_key_owes_no_other_host():
    """The rejecting half: the tick's own run is the only apply a `daniel-box` key needs."""
    reader = KeyReader(_PIN_READER, frozenset(), "daniel-box")
    assert (
        land_reach.remaining_setup_hosts([], "daniel-box", key_readers=[reader]) == {}
    )


def test_a_shipper_reading_none_of_the_keys_is_left_out():
    """`unread` drops a role as `route_key_readers` does, so the tick and this agree."""
    reader = KeyReader(_PIN_READER, frozenset({"initial_setup"}))
    assert (
        land_reach.remaining_setup_hosts([], "daniel-box", key_readers=[reader]) == {}
    )
