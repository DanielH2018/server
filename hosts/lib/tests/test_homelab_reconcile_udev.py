"""`hosts/lib/homelab-reconcile`'s udev arm (H4.3b1).

Each test is one of H4's *Verification* reconcile cases (docs/target-architecture/host/
h4-secrets-unit.md), run against the stub `udevadm` of `_reconcile_host.py`.

Run: uv run pytest hosts/lib/tests/test_homelab_reconcile_udev.py
"""

import pytest
from _reconcile_arms import red
from _reconcile_host import Node

RULE = "99-homelab-nut-apc.rules"
RELOAD = ["control", "--reload-rules"]
TRIGGER = ["trigger", "--subsystem-match=usb"]


@pytest.fixture
def node(tmp_path) -> Node:
    return Node(tmp_path)


def settled(node: Node):
    result = node.run()
    assert result.returncode == 0, result.stderr
    return result


def test_a_package_with_no_rule_makes_no_udevadm_call(node):
    # A rule that is not a homelab package's is not this arm's.
    node.rule("70-other.rules")
    settled(node)
    assert node.udevadm() == []
    assert "udev" not in node.applied()


def test_a_new_rule_reloads_then_triggers_once_and_an_unchanged_rule_does_not(node):
    node.rule(RULE)
    settled(node)
    assert node.udevadm() == [RELOAD, TRIGGER]
    assert list(node.applied()["udev"]) == [f"/usr/lib/udev/rules.d/{RULE}"]
    settled(node)
    assert node.udevadm() == []


def test_a_rule_whose_hash_changed_reloads_and_triggers_again(node):
    node.rule(RULE)
    settled(node)
    node.rule(RULE, 'ACTION=="add", MODE="0660"\n')
    settled(node)
    assert node.udevadm() == [RELOAD, TRIGGER]


def dropped_rule_scenario(node: Node) -> None:
    node.rule(RULE)
    settled(node)
    node.drop_rule(RULE)
    settled(node)
    assert node.udevadm() == []
    assert "udev" not in node.applied()
    # Shipped again, the rule is new again.
    node.rule(RULE)
    settled(node)
    assert node.udevadm() == [RELOAD, TRIGGER]


def test_a_dropped_rule_makes_no_udevadm_call_and_leaves_the_record(node):
    dropped_rule_scenario(node)


def test_an_arm_that_triggers_on_a_dropped_rule_is_flagged(tmp_path):
    # Red fixture: comparing the whole map reads a drop as a change, so it calls udevadm.
    red(
        tmp_path,
        dropped_rule_scenario,
        "reconcile_lib/udev.py",
        "if all(known.get(path) == digest for path, digest in shipped.items()):",
        "if known == shipped:",
    )


def test_a_failed_trigger_fails_the_run_and_the_next_run_retries_it(node):
    node.rule(RULE)
    node.state["fail"] = ["udevadm trigger"]
    result = node.run()
    assert result.returncode == 1
    assert node.udevadm() == [RELOAD, TRIGGER]
    assert "udev" not in node.applied()
    node.state["fail"] = []
    settled(node)
    assert node.udevadm() == [RELOAD, TRIGGER]
