"""Reconcile's ufw arm, against a stub `ufw` that replays what ufw 0.36.2-6 did when measured (H7.2b).

The stub is `_reconcile_stub.py:ufw`. The clauses below are H7's *Red proofs for the checks this
slice adds*, ufw arm. Each red fixture is a copy of reconcile with one rule removed
(`_reconcile_arms.red`), and its scenario must fail against it. A run that ends `ok` is what
homelab-updater accepts as reconciled, and a failed run is what it holds the host on
(test_homelab_updater.py), so these tests read `applied()["result"]` and not the updater.

Run: uv run pytest hosts/lib/tests/test_reconcile_ufw_arm.py
"""

import pytest
from _reconcile_arms import base, metrics, red
from _reconcile_host import V1, Node

ARM = "reconcile_lib/ufw.py"
LAN = "allow from 192.0.2.0/24 to any port 22 proto tcp"
LAN_SSH = {
    "id": "lan-ssh",
    "spec": LAN,
    "keep": True,
    "position": "first",
    "before": "ssh-limit",
}
SSH_LIMIT = {
    "id": "ssh-limit",
    "spec": "limit 22/tcp",
    "keep": True,
    "position": "last",
    "before": None,
}
WEB = {
    "id": "web",
    "spec": "allow 80/tcp",
    "keep": False,
    "position": "last",
    "before": None,
}
RULES = (LAN_SSH, SSH_LIMIT, WEB)
LABELS = {
    LAN: "homelab:keep:lan-ssh",
    "limit 22/tcp": "homelab:keep:ssh-limit",
    "allow 80/tcp": "homelab:web",
}


@pytest.fixture
def node(tmp_path) -> Node:
    return base(Node(tmp_path))


def declare(node: Node, *rules: dict, legacy: tuple = ()) -> None:
    node.manifest["reconcilers"] = ["ufw"]
    node.manifest["ufw"] = {"rules": list(rules), "legacy": list(legacy)}


def have(node: Node, *rules: tuple[str, str], active: bool = True) -> None:
    node.state["ufw"] = {
        "active": active,
        "rules": [list(r) for r in rules],
        "fail": [],
    }


def added(node: Node) -> list[tuple[str, str]]:
    return [tuple(rule) for rule in node.state["ufw"]["rules"]]


def ufw_calls(node: Node) -> list[list[str]]:
    return [call["argv"][1:] for call in node.calls("ufw")]


def writes(node: Node) -> list[list[str]]:
    reads = (
        ["show", "added"],
        ["status"],
        ["status", "numbered"],
        ["status", "verbose"],
    )
    return [argv for argv in ufw_calls(node) if argv not in reads]


def drift(node: Node) -> str:
    return 'homelab_host_reconcile_drift{host="daniel-pi",arm="ufw"}'


# What is and is not run.


def test_a_host_whose_manifest_declares_no_ufw_runs_no_ufw_command_at_all(node):
    have(node, ("allow 9/tcp", ""), active=False)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("ufw") == []
    assert "ufw" not in node.applied()
    assert 'arm="ufw"' not in metrics(node)


# Steps 1 and 2: the policies, the adds, the adoption of rules that predate the arm.


def test_declared_rules_are_added_with_their_labels_and_the_defaults_are_set(node):
    have(node)
    declare(node, *RULES)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert added(node) == [(spec, label) for spec, label in LABELS.items()]
    assert node.state["ufw"]["defaults"] == ["deny incoming", "allow outgoing"]
    assert not [argv for argv in ufw_calls(node) if "routed" in argv]
    assert node.applied()["ufw"] == {"ids": ["lan-ssh", "ssh-limit", "web"]}


def test_a_first_position_rule_is_inserted_at_the_top_of_a_non_empty_list(node):
    have(node, ("allow 9/tcp", ""))
    declare(node, LAN_SSH, SSH_LIMIT)

    node.run()

    assert added(node)[0] == (LAN, LABELS[LAN])
    assert ["insert", "1", *LAN.split(), "comment", LABELS[LAN]] in ufw_calls(node)


def test_a_first_position_rule_on_an_empty_list_is_appended_not_inserted(node):
    have(node)
    declare(node, LAN_SSH, SSH_LIMIT)

    node.run()

    assert not [argv for argv in ufw_calls(node) if argv[:1] == ["insert"]]


def test_a_rule_that_predates_the_arm_is_adopted_in_place_by_appending_it_again(node):
    # ufw prints "Skipping inserting existing rule" for `insert` and keeps the old comment, so a
    # first-position rule that exists is relabelled with the append form.
    have(node, (LAN, ""), ("limit 22/tcp", ""))
    declare(node, LAN_SSH, SSH_LIMIT)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert added(node) == [(LAN, LABELS[LAN]), ("limit 22/tcp", LABELS["limit 22/tcp"])]
    assert not [argv for argv in ufw_calls(node) if argv[:1] == ["insert"]]


def second_run_writes_nothing(node: Node) -> None:
    # `ufw default` on an active firewall stops and starts every chain even when the policy is
    # unchanged, which is the window a kube-router sync wiped the before chains in on 2026-10-25.
    have(node)
    declare(node, *RULES)
    node.run()

    node.run()

    assert writes(node) == []


def test_a_second_run_changes_nothing(node):
    second_run_writes_nothing(node)


def test_red_an_arm_that_sets_the_defaults_every_run_is_caught(tmp_path):
    red(
        tmp_path,
        second_run_writes_nothing,
        ARM,
        "        if live.get(direction) != policy:",
        "        if True:",
    )


def test_an_inactive_host_gets_both_defaults_because_it_prints_none(node):
    have(node, active=False)
    node.state["ufw"]["policy"] = {"incoming": "deny", "outgoing": "allow"}
    declare(node, *RULES)

    node.run()

    assert node.state["ufw"]["defaults"] == ["deny incoming", "allow outgoing"]


# Step 3: the order check.


def lan_below_limit(node: Node) -> None:
    have(
        node, ("limit 22/tcp", LABELS["limit 22/tcp"]), (LAN, LABELS[LAN]), active=False
    )
    declare(node, LAN_SSH, SSH_LIMIT)

    result = node.run()

    assert result.returncode == 1
    assert "ufw order: lan-ssh must sort above ssh-limit" in result.stderr
    assert "not present with its label" not in result.stderr
    assert node.applied()["result"]["ok"] is False
    assert not [argv for argv in ufw_calls(node) if argv[:1] == ["delete"]], (
        "never repaired by deleting"
    )
    assert ["--force", "enable"] not in ufw_calls(node)


def test_an_allow_rule_below_its_before_rule_fails_the_run_on_an_inactive_host(node):
    lan_below_limit(node)


def test_red_an_order_check_that_reads_ufw_status_numbered_sees_nothing_on_an_inactive_host(
    tmp_path,
):
    red(
        tmp_path,
        lan_below_limit,
        ARM,
        'run("ufw", "show", "added")',
        'run("ufw", "status", "numbered")',
    )


# Step 4: legacy rules.


LEGACY_WEB = {"spec": "allow 8080/tcp", "comment": "deploy_ui", "replaced_by": "web"}


def test_a_legacy_rule_is_deleted_only_after_its_replacement_is_present(node):
    have(node, ("allow 8080/tcp", "deploy_ui"))
    declare(node, WEB, legacy=(LEGACY_WEB,))

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert added(node) == [("allow 80/tcp", "homelab:web")]


def test_a_legacy_rule_stays_when_its_replacement_could_not_be_added(node):
    have(node, ("allow 8080/tcp", "deploy_ui"))
    node.state["ufw"]["fail"] = ["allow 80/tcp comment homelab:web"]
    declare(node, WEB, legacy=(LEGACY_WEB,))

    result = node.run()

    assert result.returncode == 1
    assert ("allow 8080/tcp", "deploy_ui") in added(node)


def adopted_spec_survives(node: Node) -> None:
    # The live hosts carry `limit 22/tcp` with no comment. The arm relabels it, and the legacy
    # entry for the Ansible-era rule names the same spec, so deleting it would delete the keep rule.
    have(node, (LAN, ""), ("limit 22/tcp", ""))
    legacy = {"spec": "limit 22/tcp", "comment": "", "replaced_by": "ssh-limit"}
    declare(node, LAN_SSH, SSH_LIMIT, legacy=(legacy,))

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert ("limit 22/tcp", "homelab:keep:ssh-limit") in added(node)


def test_a_legacy_entry_naming_an_adopted_spec_never_deletes_the_keep_rule(node):
    adopted_spec_survives(node)


def test_red_a_legacy_step_that_deletes_the_adopted_spec_removes_the_keep_rule(
    tmp_path,
):
    red(
        tmp_path,
        adopted_spec_survives,
        ARM,
        'if entry["spec"] in declared or (',
        "if (",
    )


def test_an_uncommented_rule_no_legacy_entry_names_is_left_alone(node):
    have(node, ("allow 9/tcp", ""), ("allow 8080/tcp", "deploy_ui"))
    declare(node, WEB, legacy=(LEGACY_WEB,))

    node.run()

    assert ("allow 9/tcp", "") in added(node)


# Step 5: the floor.


def stale(count: int, keep: bool = False) -> list[tuple[str, str]]:
    prefix = "homelab:keep:" if keep else "homelab:"
    return [(f"allow {7000 + i}/tcp", f"{prefix}old{i}") for i in range(count)]


def three_stale(node: Node) -> None:
    have(node, *stale(3))
    declare(node, WEB, LAN_SSH, SSH_LIMIT)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert stale(3)[2] in added(node), "the third deletion waits for the next run"
    assert [rule for rule in added(node) if rule[1].startswith("homelab:old")] == [
        stale(3)[2]
    ]
    assert f"{drift(node)} 1" in metrics(node)


def test_at_most_two_undeclared_rules_are_deleted_in_one_run_and_the_third_is_drift(
    node,
):
    three_stale(node)


def test_the_next_run_deletes_the_rule_the_floor_kept(node):
    three_stale(node)

    node.run()

    assert not [rule for rule in added(node) if rule[1].startswith("homelab:old")]
    assert f"{drift(node)} 0" in metrics(node)


def test_a_keep_rule_that_is_no_longer_declared_is_drift_and_is_never_deleted(node):
    have(node, *stale(1, keep=True))
    declare(node, WEB, LAN_SSH, SSH_LIMIT)

    node.run()

    assert stale(1, keep=True)[0] in added(node)
    assert f"{drift(node)} 1" in metrics(node)


def revert_deletes_nothing(node: Node) -> None:
    have(node)
    declare(node, *RULES)
    assert node.run().returncode == 0
    node.manifest.pop("ufw")
    node.manifest["reconcilers"] = []
    before = added(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert added(node) == before, (
        "an empty declared set deletes nothing, keep rules included"
    )
    assert node.applied()["result"]["ok"] is True
    assert node.applied()["result"]["versions"] == {
        "homelab-updater": V1,
        "homelab-daniel-pi": V1,
    }
    assert f"{drift(node)} 3" in metrics(node)


def test_a_revert_that_drops_every_declaration_deletes_nothing_and_the_run_succeeds(
    node,
):
    revert_deletes_nothing(node)


def test_red_a_floor_hit_treated_as_a_failure_would_hold_a_reverted_host(tmp_path):
    red(
        tmp_path,
        revert_deletes_nothing,
        ARM,
        "return len(stale) - len(deleted), ok",
        "return len(stale) - len(deleted), ok and len(stale) == len(deleted)",
    )


# Step 6: enabling.


def fresh_host(node: Node) -> None:
    have(node, active=False)
    declare(node, *RULES)


def enabled_after_the_ssh_rules(node: Node) -> None:
    fresh_host(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.state["ufw"]["active"] is True
    calls = writes(node)
    enable = calls.index(["--force", "enable"])
    adds = [i for i, argv in enumerate(calls) if "comment" in argv]
    assert len(adds) == 3 and max(adds) < enable, (
        "ssh must be allowed before ufw is enabled"
    )


def test_an_inactive_host_is_enabled_only_after_every_keep_rule_was_added(node):
    enabled_after_the_ssh_rules(node)


def test_red_an_arm_that_enables_before_adding_the_ssh_rules_is_caught(tmp_path):
    red(
        tmp_path,
        enabled_after_the_ssh_rules,
        ARM,
        "    if rules:\n        ok = set_defaults()",
        '    if rules:\n        ufw("--force", "enable")\n        ok = set_defaults()',
    )


def test_red_an_arm_that_never_enables_leaves_a_rebuilt_host_open(tmp_path):
    red(
        tmp_path,
        enabled_after_the_ssh_rules,
        ARM,
        'if not status.stdout.startswith("Status: inactive"):',
        "if True:",
    )


def test_an_active_host_is_never_enabled_again(node):
    have(node, active=True)
    declare(node, *RULES)

    node.run()

    assert ["--force", "enable"] not in ufw_calls(node)


def only_web_declared(node: Node) -> None:
    # Over this set "every keep rule is present" is vacuously true.
    have(node, active=False)
    declare(node, WEB)

    result = node.run()

    assert result.returncode == 1
    assert node.state["ufw"]["active"] is False
    assert "keep rules" in result.stderr


def test_ufw_is_not_enabled_unless_the_ssh_keep_rules_are_declared(node):
    only_web_declared(node)


def test_red_a_vacuous_keep_check_would_lock_a_rebuilt_host_out(tmp_path):
    red(
        tmp_path,
        only_web_declared,
        ARM,
        "absent = [i for i in REQUIRED_KEEP if i not in keep]",
        "absent = []",
    )


def test_a_failed_enable_fails_the_run_and_the_host_holds(node):
    fresh_host(node)
    node.state["ufw"]["fail"] = ["--force enable"]

    result = node.run()

    assert result.returncode == 1
    assert node.applied()["result"]["ok"] is False


def crossed_by_the_enable(node: Node) -> None:
    # The enable succeeds, and the active ruleset then lists the allow rule below the limit.
    fresh_host(node)
    node.state["ufw"]["reorder_on_enable"] = True

    result = node.run()

    assert result.returncode == 1
    assert "ufw order after enable" in result.stderr
    assert node.applied()["result"]["ok"] is False


def test_a_relation_violated_after_the_enable_is_a_fault(node):
    crossed_by_the_enable(node)


def test_red_an_arm_that_skips_the_check_on_the_active_ruleset_is_caught(tmp_path):
    red(
        tmp_path,
        crossed_by_the_enable,
        ARM,
        "    return not problems",
        "    return True",
    )


def test_the_metrics_file_names_the_ufw_arm_even_at_zero(node):
    have(node)
    declare(node, *RULES)

    node.run()

    assert f"{drift(node)} 0" in metrics(node)


# Step 7: the loaded before chain still holds what before.rules loads.


def before_chain_wiped(node: Node) -> None:
    # The 2026-10-25 shape: a restore of a snapshot taken mid-reload left only the user jump.
    have(node, *LABELS.items())
    declare(node, *RULES)
    node.state["ufw"]["before_input"] = [
        "-N ufw-before-input",
        "-A ufw-before-input -j ufw-user-input",
    ]

    result = node.run()

    assert result.returncode == 1
    assert "ufw-before-input lacks the loopback accept" in result.stderr
    assert node.applied()["result"]["ok"] is False
    assert writes(node) == [
        ["default", "deny", "incoming"],
        ["default", "allow", "outgoing"],
    ]


def test_a_before_chain_without_the_loopback_accept_holds_the_host(node):
    before_chain_wiped(node)


def test_red_an_arm_that_skips_the_before_chain_check_is_caught(tmp_path):
    red(
        tmp_path,
        before_chain_wiped,
        ARM,
        "    if LOOPBACK in chain.stdout.splitlines():",
        "    if True:",
    )


def test_an_intact_before_chain_passes_the_check(node):
    have(node, *LABELS.items())
    declare(node, *RULES)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert ["-S", "ufw-before-input"] in [c["argv"][1:] for c in node.calls("iptables")]


def test_an_inactive_host_never_reads_the_before_chain(node):
    # An inactive ufw has no ufw-before-input to read, and an empty declaration enables nothing.
    have(node, active=False)
    declare(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("iptables") == []
