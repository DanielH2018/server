"""Guards on `ops_cron_host` — the single-host gate in `initial_setup/tasks/crons.yml`.

Thirteen tasks there installed the repo-level ops crons behind `inventory_hostname ==
'daniel-box'`, so moving those crons to another host meant editing thirteen literals and the
page that documents them (#2861). One variable makes it one edit.

WHERE THE VARIABLE LIVES IS PART OF THE GUARD. `scripts/deploy_tools/land_reach.py:_eval_when`
resolves a gate against `group_vars/all.yml` + `host_vars` only, and returns "every host"
for a name it cannot resolve. Moved to the role's `defaults/`, this gate would put the
over-broad verdict of issue #2073 back on every change to this file.

THE CENSUS GLOBS FOR ITS OWN SUBJECT, so KNOWN_GATED_TASKS names members it must find: a
rename that emptied the census would pass the literal assertion over nothing.

Run: uv run pytest ansible/tests/setup/test_ops_cron_host_gate.py
"""

import re

from _helpers import ALL_VARS, ANSIBLE
from land_reach import _eval_when
from lib import yaml_fast

VAR = "ops_cron_host"
CRONS = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
_HOST_LITERAL = re.compile(r"inventory_hostname\s*==\s*['\"]")

# Named rather than counted, so a rename fails with the member that went missing.
KNOWN_GATED_TASKS = frozenset(
    {
        "Schedule the weekly worktree sweep and git object-store repair (box only)",
        "Schedule the infrastructure-map refresh (box only)",
        "Schedule the generated-docs refresh (box only)",
        "Schedule the weekly homelab eval sweep (box only)",
    }
)


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


def test_the_single_host_gate_names_the_variable_not_a_literal():
    gated = _gated_tasks()
    assert KNOWN_GATED_TASKS <= set(gated), KNOWN_GATED_TASKS - set(gated)
    literals = {
        name: when for name, when in gated.items() if _HOST_LITERAL.search(when)
    }
    assert not literals, f"host names hard-coded in a gate: {literals}"
    assert [when for when in gated.values() if VAR in when]


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
