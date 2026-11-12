"""`runbook_gates.py metallb-upgrade` against a fake kubectl: each gate's clean/flagged pair.

Both gates are `k3s_upgrade.py`'s verdicts, so this file proves the wiring and the numbering,
not the verdicts themselves, which `test_runbook_gates_k3s_upgrade.py` covers.

Run: uv run pytest scripts/deploy_tools/tests/test_runbook_gates_metallb_upgrade.py
"""

import io

from _gates_fakes import fake_tools
from deploy_tools.runbook_gates_lib import k3s_upgrade, metallb_upgrade as gates


def _node(name: str, ready: str = "True") -> dict:
    return {
        "metadata": {"name": name},
        "status": {"conditions": [{"type": "Ready", "status": ready}]},
    }


BOTH_READY = {"items": [_node("daniel-box"), _node("daniel-server")]}


def _run(read_hold=lambda: "none", nodes=BOTH_READY) -> tuple[int, str]:
    out = io.StringIO()
    tools = fake_tools({k3s_upgrade.NODES_ARGS: nodes})
    code = gates.run_gates(tools=tools, read_hold=read_hold, out=out)
    return code, out.getvalue()


def test_a_whole_cluster_with_no_hold_is_clean():
    code, out = _run()
    assert code == 0, out
    assert "all 2 gates passed" in out


def test_a_deploy_hold_is_flagged_as_gate_1():
    code, out = _run(lambda: "a" * 40 + " failed=sonarr")
    assert code == 1, out
    assert "a" * 40 in out


def test_a_missing_deploy_hold_is_flagged_as_gate_1():
    code, out = _run(lambda: None)
    assert code == 1, out


def test_a_notready_node_is_flagged_as_gate_2():
    code, out = _run(
        nodes={"items": [_node("daniel-box"), _node("daniel-server", "False")]},
    )
    assert code == 2, out
    assert "daniel-server (Ready=False)" in out
