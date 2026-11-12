"""Guards on `ops_cron_host`, the single-host gate `group_vars/all.yml` holds.

The repo-level ops jobs sat behind one variable instead of an `inventory_hostname ==
'daniel-box'` literal, so moving them to another host was one edit. They are host components
since H7.10, and H7.63 deleted `initial_setup/tasks/crons.yml`, so no task names the variable;
`scripts/docs/reference/crons.py` and the worktree-sweep component still read it.

WHERE THE VARIABLE LIVES IS PART OF THE GUARD. `scripts/deploy_tools/setup_lib/setup_gates.py:_eval_when`
resolves a gate against `group_vars/all.yml` + `host_vars` only, and returns "every host"
for a name it cannot resolve. Moved to a role's `defaults/`, this gate would put the
over-broad verdict back.

Run: uv run pytest ansible/tests/setup/test_ops_cron_host_gate.py
"""

from _helpers import ALL_VARS
from deploy_tools.setup_lib.setup_gates import _eval_when
from lib import yaml_fast

VAR = "ops_cron_host"


def test_the_reach_reader_still_narrows_the_gate_to_one_host():
    """The reader itself, not its precondition.

    `_eval_when` resolves a gate against `group_vars/all.yml` + `host_vars` and fails OPEN,
    so the variable moved into a role's `defaults/` would read as every host while
    `group_vars` still held the old value and every assertion here passed.
    """
    assert yaml_fast.safe_load(ALL_VARS.read_text()).get(VAR) == "daniel-box"
    gate = f"inventory_hostname == {VAR}"
    assert _eval_when(gate, "daniel-box") is True
    assert _eval_when(gate, "daniel-pi") is False
