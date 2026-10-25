"""Guards that an initial_setup apply never reloads an active UFW, and that it checks the loaded chain.

`ufw enable` on an active firewall still runs ufw-init start, which flushes and reloads every
ufw chain. On a k3s node a kube-router save and restore of the filter table inside that window
writes the flushed chains back, and on 2026-10-25 daniel-box lost loopback that way. So
`Enable UFW firewall` must be gated on the status read showing an inactive firewall, and the
play must end with a read-only assert over the live ufw-before-input chain.

Run: uv run pytest ansible/tests/setup/test_ufw_enable_skips_an_active_firewall.py
"""

from _helpers import ANSIBLE
from lib import yaml_fast

NETWORK = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "network.yml"

STATUS_TASK = "Read whether UFW is active"
ENABLE_TASK = "Enable UFW firewall"
CHAIN_TASK = "Read the loaded UFW before-input chain"
ASSERT_TASK = "Fail when UFW's before-input chain lost the loopback accept"
LOOPBACK = "-A ufw-before-input -i lo -j ACCEPT"


def _tasks() -> list[dict]:
    return [
        t for t in yaml_fast.safe_load(NETWORK.read_text()) or [] if isinstance(t, dict)
    ]


def _task(name: str) -> tuple[int, dict]:
    for index, task in enumerate(_tasks()):
        if task.get("name") == name:
            return index, task
    raise AssertionError(f"census found no task named {name!r} in {NETWORK.name}")


def _gated_on_inactive(task: dict, register: str) -> bool:
    """True when `task` runs only while the `register` status read says inactive."""
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return any(
        isinstance(c, str)
        and c.startswith(register)
        and "Status: inactive" in c
        and "not " not in c
        for c in conditions
    )


def test_the_enable_runs_only_on_an_inactive_firewall_is_clean():
    status_at, status = _task(STATUS_TASK)
    enable_at, enable = _task(ENABLE_TASK)
    assert status["ansible.builtin.command"]["cmd"] == "ufw status"
    assert status.get("check_mode") is False
    assert status_at < enable_at
    assert _gated_on_inactive(enable, status["register"])


def test_an_enable_that_runs_on_an_active_firewall_is_flagged():
    register = "initial_setup_ufw_status"
    assert not _gated_on_inactive(
        {"community.general.ufw": {"state": "enabled"}}, register
    )
    assert not _gated_on_inactive(
        {"when": "other_status.stdout is search('Status: inactive')"}, register
    )
    assert not _gated_on_inactive(
        {"when": f"not {register}.stdout is search('Status: inactive')"}, register
    )


def test_the_play_asserts_the_loopback_accept_after_the_enable():
    enable_at, _ = _task(ENABLE_TASK)
    chain_at, chain = _task(CHAIN_TASK)
    assert_at, check = _task(ASSERT_TASK)
    assert enable_at < chain_at < assert_at
    assert chain["ansible.builtin.command"]["cmd"] == "iptables -S ufw-before-input"
    assert chain.get("changed_when") is False
    assert chain.get("check_mode") is False
    that = check["ansible.builtin.assert"]["that"]
    assert any(LOOPBACK in line and chain["register"] in line for line in that)
