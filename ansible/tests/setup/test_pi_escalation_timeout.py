"""Guard on daniel-pi's `ansible_ssh_timeout` — the escalation-prompt budget for the Pi.

An apply against the 456 MB Zero 2 W intermittently failed at Gathering Facts with
`Timeout (12s) waiting for privilege escalation prompt` and read as UNREACHABLE (#1747). The
12 s is `2 + timeout` in ansible-core's ssh connection plugin, and the per-host spelling of
that option is `ansible_ssh_timeout` — NOT `ansible_become_timeout`, which the pinned core does
not define, so a var by that name is a placebo that reads as a fix. The accepting half asserts
the var is present and at least four times the observed floor; the rejecting half asserts the
placebo name is absent, so a well-meant rename fails with the name that came back.

Run: uv run pytest ansible/tests/setup/test_pi_escalation_timeout.py
"""

from pathlib import Path

import ansible.plugins.connection.ssh as ssh_plugin
from _helpers import HOST_VARS
from lib import yaml_fast

PI_VARS = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())

# The ssh plugin waits 2 + timeout; the fleet default of 10 gave the 12 s in the report.
VAR = "ansible_ssh_timeout"
PLACEBO = "ansible_become_timeout"
FLOOR = 30

SSH_PLUGIN = Path(ssh_plugin.__file__)


def test_pi_raises_the_escalation_timeout_above_the_fleet_default():
    assert VAR in PI_VARS, (
        f"{VAR} missing from host_vars/daniel-pi.yml — #1747 regresses"
    )
    assert PI_VARS[VAR] >= FLOOR, f"{VAR}={PI_VARS[VAR]} is under the {FLOOR}s floor"


def test_pi_does_not_carry_the_become_timeout_placebo():
    assert PLACEBO not in PI_VARS, (
        f"{PLACEBO} is not an ansible-core option; the ssh plugin reads {VAR}"
    )


def test_the_pinned_core_still_derives_the_prompt_wait_from_the_ssh_timeout():
    """The derivation the host_vars comment cites must still be in the pinned core."""
    source = SSH_PLUGIN.read_text()
    assert "timeout = 2 + self.get_option('timeout')" in source
    assert "waiting for privilege escalation prompt" in source
