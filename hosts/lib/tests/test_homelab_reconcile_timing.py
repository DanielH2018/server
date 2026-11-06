"""`hosts/lib/homelab-reconcile` says in the journal how long each action took (#7490).

Each test makes one stub command slow by rewriting its wrapper, so the time a line must report
comes from the test. Reconcile's stderr is its journal stream. A line counts when it names the
action and carries a duration such as `1.5s`, `1500ms` or `1.5 seconds`; word order is free.

Run: uv run pytest hosts/lib/tests/test_homelab_reconcile_timing.py
"""

import fcntl
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest
from _reconcile_arms import base, ship
from _reconcile_host import Node

SLOW_SECONDS = 1.5
# Below the injected delay, because a whole-second log format rounds 1.5 down to 1.
AT_LEAST = 1.0
DURATION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(ms|s|secs?|seconds?)\b")
PRIORITY = re.compile(r"^<(\d)>")
NOTICE = 5  # journald stores notice and above on this host (MaxLevelStore=notice)
SLOW_UNIT = "homelab-a-slow.service"
FAST_UNIT = "homelab-b-fast.service"


@pytest.fixture
def node(tmp_path) -> Node:
    return base(Node(tmp_path))


def slowed(node: Node, command: str, args: str, action: str = "") -> None:
    """Make `command` run `action` (by default a sleep) first when called with exactly `args`."""
    wrapper = node.bin / command
    shebang, body = wrapper.read_text().split("\n", 1)
    action = action or f"sleep {SLOW_SECONDS}"
    wrapper.write_text(
        f'{shebang}\ncase "$*" in {shlex.quote(args)}) {action} ;; esac\n{body}'
    )


def seconds_on(line: str) -> list[float]:
    return [
        float(value) / 1000 if unit == "ms" else float(value)
        for value, unit in DURATION.findall(line)
    ]


def timed_lines(stderr: str, subject: str) -> list[str]:
    return [
        line for line in stderr.splitlines() if subject in line and seconds_on(line)
    ]


def longest(stderr: str, subject: str) -> float:
    """The longest duration on a line naming `subject`; fails when no such line exists."""
    lines = timed_lines(stderr, subject)
    assert lines, f"no line names {subject!r} with a duration:\n{stderr}"
    return max(seconds for line in lines for seconds in seconds_on(line))


def settled_with_units(node: Node, *units: str) -> None:
    """Ship active `units` and run once, so the next run has a previous state to compare with."""
    for unit in units:
        node.unit(unit, active="active")
    result = node.run()
    assert result.returncode == 0, result.stderr


def change(node: Node, *units: str) -> None:
    for unit in units:
        node.write_unit(unit, "[Unit]\nDescription=changed\n")


def test_each_restart_reports_its_own_time_against_its_own_unit(node):
    settled_with_units(node, SLOW_UNIT, FAST_UNIT)
    change(node, SLOW_UNIT, FAST_UNIT)
    slowed(node, "systemctl", f"restart {SLOW_UNIT}")

    stderr = node.run().stderr

    slow = longest(stderr, SLOW_UNIT)
    assert slow >= AT_LEAST
    # The fast unit restarts second, so a clock that never resets reports it as the slower one.
    assert longest(stderr, FAST_UNIT) < slow


def test_a_restart_that_fails_still_reports_its_time(node):
    settled_with_units(node, SLOW_UNIT)
    change(node, SLOW_UNIT)
    slowed(node, "systemctl", f"restart {SLOW_UNIT}")
    node.state["fail"].append(f"restart {SLOW_UNIT}")

    stderr = node.run().stderr

    assert longest(stderr, SLOW_UNIT) >= AT_LEAST


def test_stopping_a_dropped_unit_reports_its_time(node):
    settled_with_units(node, SLOW_UNIT)
    node.drop(SLOW_UNIT)
    slowed(node, "systemctl", f"stop {SLOW_UNIT}")

    assert longest(node.run().stderr, SLOW_UNIT) >= AT_LEAST


def test_starting_an_enabled_unit_reports_its_time(node):
    node.unit(SLOW_UNIT, active="active", linked=True)
    assert node.run().returncode == 0
    node.state["units"][SLOW_UNIT]["active"] = "inactive"
    slowed(node, "systemctl", f"start {SLOW_UNIT}")

    assert longest(node.run().stderr, SLOW_UNIT) >= AT_LEAST


def test_restarting_homelab_secrets_reports_its_time(node):
    node.secrets()
    slowed(node, "systemctl", "restart homelab-secrets.service")

    assert longest(node.run().stderr, "homelab-secrets.service") >= AT_LEAST


def test_an_activation_reports_its_time(node):
    ship(node, "/etc/sysctl.d/50-homelab.conf", "kernel.sysrq = 0\n", "sysctl")
    slowed(node, "sysctl", "--system")

    assert longest(node.run().stderr, "sysctl") >= AT_LEAST


def test_retiring_a_legacy_unit_reports_its_time(node):
    node.manifest["legacy"].append({"unit": SLOW_UNIT})
    slowed(node, "systemctl", f"stop {SLOW_UNIT}")

    assert longest(node.run().stderr, SLOW_UNIT) >= AT_LEAST


def test_the_ufw_arm_reports_its_time(node):
    node.manifest["reconcilers"] = ["ufw"]
    node.manifest["ufw"] = {
        "rules": [
            {
                "id": "web",
                "spec": "allow 80/tcp",
                "keep": False,
                "position": "last",
                "before": None,
            }
        ],
        "legacy": [],
    }
    slowed(node, "ufw", "show added")

    assert longest(node.run().stderr, "ufw") >= AT_LEAST


def test_the_udev_arm_reports_its_time(node):
    node.rule("60-homelab-test.rules")
    slowed(node, "udevadm", "control --reload-rules")

    assert longest(node.run().stderr, "udev") >= AT_LEAST


def _waiting_on(lock: Path) -> bool:
    inode = lock.stat().st_ino
    with open("/proc/locks") as locks:
        return any("->" in line and f":{inode} " in line for line in locks)


def test_the_wait_for_the_dpkg_lock_reports_its_time(node):
    # A postinst starts reconcile during the apt transaction, so this wait spends the same budget.
    with node.lock.open("r+") as held:
        fcntl.lockf(held, fcntl.LOCK_EX)
        process = node.start()
        deadline = time.monotonic() + 30
        while not _waiting_on(node.lock):
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "reconcile never waited on the lock"
            time.sleep(0.01)
        time.sleep(SLOW_SECONDS)

    _, stderr = process.communicate(timeout=60)

    assert longest(stderr, "lock") >= AT_LEAST


def test_a_run_killed_during_an_action_has_already_named_that_action(node, tmp_path):
    # systemd's timeout ends the run with SIGTERM, so a line written after the call never appears.
    settled_with_units(node, SLOW_UNIT)
    change(node, SLOW_UNIT)
    entered = tmp_path / "entered"
    slowed(
        node,
        "systemctl",
        f"restart {SLOW_UNIT}",
        f"touch {shlex.quote(str(entered))}; sleep 3; exit 1",
    )
    process = node.start()
    deadline = time.monotonic() + 30
    while not entered.exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, "reconcile never reached the restart"
        time.sleep(0.01)

    process.send_signal(signal.SIGTERM)
    _, stderr = process.communicate(timeout=60)

    assert SLOW_UNIT in stderr


def test_a_timing_line_is_at_a_priority_this_hosts_journal_stores(node, tmp_path):
    settled_with_units(node, SLOW_UNIT)
    change(node, SLOW_UNIT)
    argv, env = node.command()
    journal = tmp_path / "journal"
    with journal.open("w") as stream:
        status = os.fstat(stream.fileno())
        env["JOURNAL_STREAM"] = f"{status.st_dev}:{status.st_ino}"
        subprocess.run(
            argv, env=env, stdout=subprocess.DEVNULL, stderr=stream, timeout=60
        )

    lines = timed_lines(journal.read_text(), SLOW_UNIT)

    assert lines, f"no line names {SLOW_UNIT} with a duration"
    for line in lines:
        prefix = PRIORITY.match(line)
        assert prefix is None or int(prefix[1]) <= NOTICE, line
