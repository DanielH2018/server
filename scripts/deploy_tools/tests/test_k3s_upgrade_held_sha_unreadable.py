"""An unreadable deployer marker makes `runbook_gates.py k3s-upgrade` unavailable, not gate 3 (#4468).

`held_sha` reads a local file, so it does not go through `gate_runner.cluster_doc` and its
`Unreadable`. Returning a placeholder offender for a marker it could not open makes the runner
exit with the hold gate's number, which reads as "a hold is set" when nothing was read. The
cluster reads already reach `EX_UNAVAILABLE` (69); this one has to as well.

Run: uv run pytest scripts/deploy_tools/tests/test_k3s_upgrade_held_sha_unreadable.py
"""

import io

from _gates_fakes import BOTH_READY, fake_tools
from deploy_tools.runbook_gates_lib import k3s_upgrade as gates
from gitops_markers import MARKERS


def test_an_unreadable_ledger_exits_unavailable_not_the_hold_gate(tmp_path):
    state_dir = tmp_path / "gitops-deploy"
    state_dir.mkdir()
    # A directory in the ledger's place fails the read for any user, root included.
    (state_dir / MARKERS["owed"]).mkdir()
    tools = fake_tools(
        {
            gates.VOLUMES_ARGS: {"items": []},
            gates.BACKUPS_ARGS: {"items": []},
            gates.NODES_ARGS: BOTH_READY,
        }
    )
    out = io.StringIO()
    code = gates.run_gates(tools=tools, state_dir=str(state_dir), out=out)
    assert code == gates.EX_UNAVAILABLE, out.getvalue()
    assert str(state_dir) in out.getvalue()


def test_an_unreadable_ledger_is_not_reported_as_a_cluster_read(tmp_path):
    """The marker is a local file, so the line must not send the operator to kubectl (#4485)."""
    state_dir = tmp_path / "gitops-deploy"
    state_dir.mkdir()
    (state_dir / MARKERS["owed"]).mkdir()
    tools = fake_tools(
        {
            gates.VOLUMES_ARGS: {"items": []},
            gates.BACKUPS_ARGS: {"items": []},
            gates.NODES_ARGS: BOTH_READY,
        }
    )
    out = io.StringIO()
    gates.run_gates(tools=tools, state_dir=str(state_dir), out=out)
    hold_lines = [
        line for line in out.getvalue().splitlines() if line.startswith("gate 3")
    ]
    assert hold_lines, out.getvalue()
    assert not any("cannot ask the cluster" in line for line in hold_lines), (
        out.getvalue()
    )
