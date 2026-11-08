"""No `initial_setup` task gates on a host-name literal.

A gate names the fact it reads, not the host that has it, so replacing a host edits one host_vars
entry. H7.60 deleted the last tasks that gated on a hardware flag (`has_low_memory_board`,
`has_raspi_kernel`), so the rule left is the rejecting half: a new bare literal fails until it is
recorded here.

A gate that reads HOST HISTORY rather than hardware may keep a literal behind a `DECIDED:`
comment, and none does today.

Run: uv run pytest ansible/tests/setup/test_initial_setup_host_gates.py
"""

import re

from _helpers import ANSIBLE
from _role_census import role_task_files
from lib import yaml_fast

TASKS = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks"
_HOST_LITERAL = re.compile(r"inventory_hostname\s*[!=]=\s*['\"]")

# The literals that stay, because they read host history rather than hardware. Each must sit
# behind a `DECIDED:` comment in its file.
DELIBERATE_LITERALS: frozenset[str] = frozenset()


def _when_text(task: dict) -> str:
    when = task.get("when")
    return " ".join(str(w) for w in when) if isinstance(when, list) else str(when or "")


def _gates() -> dict[str, str]:
    """Task name -> its `when:`, for every host-gated task in the role's task files."""
    gates = {}
    for path in role_task_files(TASKS.parent):
        for task in yaml_fast.safe_load(path.read_text()) or []:
            if not isinstance(task, dict):
                continue
            text = _when_text(task)
            if "inventory_hostname" in text:
                gates[str(task.get("name", "unnamed"))] = text
    return gates


def test_a_surviving_host_literal_is_one_of_the_recorded_deliberate_ones():
    """The rejecting half: any NEW bare literal fails here.

    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Hardware gates are capability flags, not host names

    Red-proof: adding `when: inventory_hostname == 'daniel-box'` to any task in this role
    and leaving it out of DELIBERATE_LITERALS fails this test with that task's name.
    """
    gates = _gates()
    assert "Deploy the setup-plane drift check" in gates, (
        "the census found no host gate"
    )
    literals = {n for n, w in gates.items() if _HOST_LITERAL.search(w)}
    assert DELIBERATE_LITERALS <= literals, DELIBERATE_LITERALS - literals
    assert not literals - DELIBERATE_LITERALS, (
        "host name hard-coded in a gate — give it a capability flag, or record the literal "
        f"as deliberate: {sorted(literals - DELIBERATE_LITERALS)}"
    )
