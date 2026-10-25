"""Guards on `ops_cron_host` — the single-host gate in `initial_setup/tasks/crons.yml`.

The repo-level ops crons sat behind one variable instead of an `inventory_hostname ==
'daniel-box'` literal, so moving them to another host was one edit. H7.10 moved the docs
refresh and the infrastructure map into host components, so no task in crons.yml names the
variable now; `scripts/docs/reference/crons.py` still reads it.

WHERE THE VARIABLE LIVES IS PART OF THE GUARD. `scripts/deploy_tools/setup_lib/setup_gates.py:_eval_when`
resolves a gate against `group_vars/all.yml` + `host_vars` only, and returns "every host"
for a name it cannot resolve. Moved to the role's `defaults/`, this gate would put the
over-broad verdict back on every change to this file.

No task gates on this variable any more: H7.10 moved the last two out. The literal check
stays, so a task that reintroduces a host name fails.

Run: uv run pytest ansible/tests/setup/test_ops_cron_host_gate.py
"""

import re

from _helpers import ALL_VARS, ANSIBLE
from deploy_tools.land_lib.land_reach import _eval_when
from lib import yaml_fast

VAR = "ops_cron_host"
CRONS = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
_HOST_LITERAL = re.compile(r"inventory_hostname\s*==\s*['\"]")


def _gated_tasks() -> dict[str, str]:
    """Task name -> its `when:`, for every task in crons.yml that gates on the host."""
    gated = {}
    for task in yaml_fast.safe_load(CRONS.read_text()) or []:
        if not isinstance(task, dict):
            continue
        when = task.get("when")
        text = " ".join(when) if isinstance(when, list) else str(when or "")
        if "inventory_hostname" in text:
            gated[str(task.get("name", "unnamed"))] = text
    return gated


def test_no_host_gate_in_crons_hard_codes_a_host_name():
    gated = _gated_tasks()
    literals = {
        name: when for name, when in gated.items() if _HOST_LITERAL.search(when)
    }
    assert not literals, f"host names hard-coded in a gate: {literals}"


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
