"""Guards on the capability flags that replaced host-name literals in `initial_setup`.

Sixteen tasks across three of the role's task files gate on a hardware fact. Each reads a flag
instead of an `inventory_hostname == '<host>'` literal, so replacing a host edits one host_vars
entry. Two flags carry those facts:
`has_low_memory_board` and `has_raspi_kernel`.

WHERE THE FLAGS LIVE IS PART OF THE GUARD, for the reason
`test_ops_cron_host_gate.py` gives: `scripts/deploy_tools/setup_lib/setup_gates.py:_eval_when` resolves a
gate against `group_vars/all.yml` + `host_vars` only, and a name it cannot resolve reads as
"every host" — an over-broad verdict. A flag moved into the role's
`defaults/` would pass every assertion about the task files and still break the reach reader.

A gate that reads HOST HISTORY rather than hardware may keep a literal behind a `DECIDED:`
comment, and none does today. This file pins that split: a new bare literal fails until it is
recorded here.

THE CENSUS GLOBS FOR ITS OWN SUBJECT, so `EXPECTED_FLAG_GATES` names members the census must
find: a rename that emptied it would pass every assertion over nothing.

Run: uv run pytest ansible/tests/setup/test_initial_setup_host_gates.py
"""

import re

from _helpers import ALL_VARS, ANSIBLE, HOST_VARS
from _role_census import role_task_files
from deploy_tools.land_lib.land_reach import _eval_when
from lib import yaml_fast

TASKS = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks"
_HOST_LITERAL = re.compile(r"inventory_hostname\s*[!=]=\s*['\"]")

HOSTS = ("daniel-box", "daniel-pi", "daniel-server")

# The effective value each flag must have on every host — the intended per-host truth.
EXPECTED_FLAGS = {
    "has_low_memory_board": {
        "daniel-box": False,
        "daniel-pi": True,
        "daniel-server": False,
    },
    "has_raspi_kernel": {
        "daniel-box": False,
        "daniel-pi": True,
        "daniel-server": False,
    },
}

# Task name -> the flag its gate must name. Named rather than counted, so a rename fails with
# the member that went missing.
EXPECTED_FLAG_GATES = {
    "Gather service facts": "has_low_memory_board",
    "Stop the hardware watchdog during provisioning (Pi)": "has_low_memory_board",
    "Secure swap file permissions (Pi)": "has_low_memory_board",
    "Format swap file (Pi)": "has_low_memory_board",
    "Create swap file (Pi)": "has_low_memory_board",
    "Persist swap file in fstab (Pi)": "has_low_memory_board",
    "Activate swap file (Pi)": "has_low_memory_board",
    "Remove apt-show-versions (daniel-pi — apt-hook tax on 512 MB)": "has_low_memory_board",
    "Install apt-show-versions (roomy hosts only)": "has_low_memory_board",
    "Install Raspberry Pi-specific packages": "has_raspi_kernel",
}

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
            if "inventory_hostname" in text or any(f in text for f in EXPECTED_FLAGS):
                gates[str(task.get("name", "unnamed"))] = text
    return gates


def test_every_hardware_gate_names_its_capability_flag():
    gates = _gates()
    missing = set(EXPECTED_FLAG_GATES) - set(gates)
    assert not missing, f"census found no such task: {missing}"
    wrong = {
        name: gates[name]
        for name, flag in EXPECTED_FLAG_GATES.items()
        if flag not in gates[name] or _HOST_LITERAL.search(gates[name])
    }
    assert not wrong, f"hardware gate not on its flag: {wrong}"


def test_a_surviving_host_literal_is_one_of_the_recorded_deliberate_ones():
    """The rejecting half: any NEW bare literal fails here.

    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Hardware gates are capability flags, not host names

    Red-proof: adding `when: inventory_hostname == 'daniel-box'` to any task in this role
    and leaving it out of DELIBERATE_LITERALS fails this test with that task's name.
    """
    gates = _gates()
    literals = {n for n, w in gates.items() if _HOST_LITERAL.search(w)}
    assert DELIBERATE_LITERALS <= literals, DELIBERATE_LITERALS - literals
    assert not literals - DELIBERATE_LITERALS, (
        "host name hard-coded in a gate — give it a capability flag, or record the literal "
        f"as deliberate: {sorted(literals - DELIBERATE_LITERALS)}"
    )


def test_the_reach_reader_resolves_each_flag_to_the_same_hosts_as_the_literal():
    """The reader itself, not its precondition — `_eval_when` fails OPEN on an unknown name."""
    defaults = yaml_fast.safe_load(ALL_VARS.read_text())
    for flag, per_host in EXPECTED_FLAGS.items():
        assert defaults.get(flag) is False, (
            f"{flag} must default false in group_vars/all.yml"
        )
        for host, expected in per_host.items():
            assert _eval_when(flag, host) is expected, (flag, host)


def test_every_host_the_table_arms_opts_in_from_its_own_host_vars():
    """Non-vacuity on the host_vars side: a flag nobody sets true gates nothing.

    A flag may arm more than one host, so the count is "at least one", and every armed host
    must carry the opt-in itself rather than inheriting it from `group_vars`.
    """
    for flag, per_host in EXPECTED_FLAGS.items():
        armed = {h for h, v in per_host.items() if v}
        assert armed, f"{flag} is true on no host, so it gates nothing"
        for host in sorted(armed):
            opt_in = (HOST_VARS / f"{host}.yml").read_text()
            assert f"{flag}: true" in opt_in, (flag, host)
