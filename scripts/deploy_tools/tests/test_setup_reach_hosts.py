"""`setup_reach_hosts.py` prints the hosts a range's setup-role change reaches beyond the tick's.

Both cases read real commits, so the derivation they run is the one the tick runs. CI clones
with full history.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_reach_hosts.py
"""

import json

import setup_reach_hosts

# Adds a guest rehearsal mode to the etcd drill, under `roles/setup/hypervisor/` alone.
_HYPERVISOR = "ee529af466"


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
