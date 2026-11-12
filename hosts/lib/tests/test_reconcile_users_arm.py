"""Reconcile's users arm, against stub `getent`, `usermod` and `systemd-sysusers` (H7.2d).

The clauses are H7's *Red proofs for the checks this slice adds*, users arm. A run that ends
`ok` is what homelab-updater accepts as reconciled, and a failed run is what it holds the host
on (`test_reconcile_hold_way_out.py` drives the updater), so these tests read
`applied()["result"]`. Each red fixture is a copy of reconcile with one rule removed
(`_reconcile_arms.red`), and its scenario must fail against it.

Run: uv run pytest hosts/lib/tests/test_reconcile_users_arm.py
"""

import pytest
from _reconcile_arms import base, metrics, red
from _reconcile_host import Node

ARM = "reconcile_lib/users.py"
CLAUDE = {
    "name": "claude",
    "uid": 1001,
    "home": "/home/claude",
    "shell": "/bin/bash",
    "comment": "Claude agent",
}
BOT = {**CLAUDE, "name": "bot", "uid": 1002, "home": "/home/bot"}
DRIFT = 'homelab_host_reconcile_drift{host="daniel-pi",arm="users"}'


@pytest.fixture
def node(tmp_path) -> Node:
    return base(Node(tmp_path))


def declare(node: Node, *users: dict) -> None:
    node.manifest["reconcilers"] = ["users"]
    node.manifest["users"] = {"users": list(users)}


def commands(node: Node) -> list[str]:
    """The arm's changing commands, as `<command> <first argument>`."""
    return [
        f"{call['argv'][0]} {call['argv'][1]}"
        for call in node.calls()
        if call["argv"][0] in ("usermod", "systemd-sysusers")
    ]


# What is and is not run.


def test_a_host_declaring_no_users_runs_no_user_command_at_all(node):
    node.state["users"] = {"claude": 1001}

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("getent") == [] and commands(node) == []
    assert "users" not in node.applied()
    assert 'arm="users"' not in metrics(node)


def test_a_missing_user_is_created_by_sysusers_and_a_second_run_changes_nothing(node):
    declare(node, CLAUDE)
    node.state["sysusers"] = {"creates": {"claude": 1001}}

    first = node.run()
    second = node.run()

    assert first.returncode == 0, first.stderr
    assert node.state["users"] == {"claude": 1001}
    assert node.applied()["users"] == {"names": ["claude"]}
    assert commands(node) == [], "the second run found the user and ran no change"
    assert second.returncode == 0, second.stderr
    assert f"{DRIFT} 0" in metrics(node)


def test_a_user_that_exists_with_the_declared_uid_needs_no_sysusers(node):
    declare(node, CLAUDE)
    node.state["users"] = {"claude": 1001}

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert commands(node) == []


# A uid mismatch is a fault.


def mismatch_fails_the_run(node: Node) -> None:
    declare(node, CLAUDE, BOT)
    node.state["users"] = {"claude": 1000}
    node.state["sysusers"] = {"creates": {"bot": 1002}}

    result = node.run()

    assert result.returncode == 1
    assert "claude has uid 1000; the package declares 1001" in result.stderr
    assert node.applied()["result"]["ok"] is False
    assert commands(node) == [], "a faulted run creates nothing"


def test_a_declared_uid_that_differs_from_the_live_uid_fails_the_run(node):
    mismatch_fails_the_run(node)


def test_red_an_arm_that_never_compares_the_uid_would_leave_the_host_unheld(tmp_path):
    red(
        tmp_path,
        mismatch_fails_the_run,
        ARM,
        'if int(fields[2]) != user["uid"]:',
        "if False:",
    )


def test_a_user_still_missing_after_sysusers_is_a_fault(node):
    declare(node, CLAUDE)
    node.state["sysusers"] = {"creates": {}}  # exits 0 and creates nothing

    result = node.run()

    assert result.returncode == 1
    assert node.applied()["result"]["ok"] is False


def test_a_failing_sysusers_is_a_fault(node):
    declare(node, CLAUDE)
    node.state["sysusers"] = {"creates": {"claude": 1001}, "fail": True}

    assert node.run().returncode == 1


# A user leaving the list is expired, never deleted.


def two_users_then_one(node: Node) -> None:
    declare(node, CLAUDE, BOT)
    node.state["sysusers"] = {"creates": {"claude": 1001, "bot": 1002}}
    assert node.run().returncode == 0
    declare(node, CLAUDE)


def test_a_user_that_leaves_the_list_is_expired_and_never_deleted(node):
    two_users_then_one(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.state["expired"] == ["bot"]
    assert [c["argv"] for c in node.calls("usermod")] == [
        ["usermod", "--expiredate", "1", "bot"]
    ]
    assert "bot" in node.state["users"], "the account stays"
    assert node.applied()["users"] == {"names": ["claude"]}
    assert node.run().returncode == 0
    assert node.calls("usermod") == [], "the next run expires nothing again"


def test_a_user_that_left_the_list_and_is_gone_is_dropped_without_usermod(node):
    two_users_then_one(node)
    del node.state["users"]["bot"]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("usermod") == []
    assert node.applied()["users"] == {"names": ["claude"]}


def test_a_failed_expiry_fails_the_run_and_the_name_stays_recorded(node):
    two_users_then_one(node)
    node.state["fail_usermod"] = True

    result = node.run()

    assert result.returncode == 1
    assert node.applied()["users"] == {"names": ["bot", "claude"]}


def revert_expires_nobody(node: Node) -> None:
    declare(node, CLAUDE, BOT)
    node.state["sysusers"] = {"creates": {"claude": 1001, "bot": 1002}}
    assert node.run().returncode == 0
    node.manifest.pop("users")
    node.manifest["reconcilers"] = []

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("usermod") == [], "an empty declared set expires nothing"
    assert node.applied()["result"]["ok"] is True
    assert node.applied()["users"] == {"names": ["bot", "claude"]}
    assert f"{DRIFT} 2" in metrics(node)


def test_a_revert_that_declares_no_users_expires_nobody_and_the_run_succeeds(node):
    revert_expires_nobody(node)


def test_red_an_arm_with_no_floor_would_expire_every_agent_user_on_a_revert(tmp_path):
    red(tmp_path, revert_expires_nobody, ARM, "if not users:", "if False:")
