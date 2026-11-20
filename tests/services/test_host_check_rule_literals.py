"""The `ClaudeCgroupNotReporting` pairs in hosts.yaml follow the inventory.

A rule file is never Jinja, so each host and cgroup the rule expects is a literal. The
thresholds beside them carry their reasons as `DECIDED:` markers in `hosts.yaml` itself.

Run: uv run pytest tests/services/test_host_check_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.repo_paths import HOST_VARS
from _setup_render import CLAUDE_CODE
from docs.fragment_readers import hosts_listing_component

RULES = K8S_ROLES / "observability" / "files" / "rules" / "hosts.yaml"


def rules_named(alert: str) -> list[dict]:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    found = [r for r in group["rules"] if r["alert"] == alert]
    assert found, alert
    return found


def test_the_cgroup_pairs_follow_the_inventory():
    """Every host listing claude-code requires fleet, and claude-rc only where its RC unit runs.

    The pairs in `ClaudeCgroupNotReporting` are literals, so this is what makes them follow the
    inventory: a host that gains Claude Code without a rule is a writer nothing checks, and a host
    whose RC unit is switched off would page for a cgroup it legitimately lacks.
    """
    # fact: k8s/roles/node-exporter/CLAUDE.md#The textfile collector directory
    rc_default = yaml_fast.safe_load(
        (CLAUDE_CODE / "defaults" / "main.yml").read_text()
    )["claude_code_rc_enabled"]
    derived = set()
    for name in hosts_listing_component("claude-code"):
        host = yaml_fast.safe_load((HOST_VARS / f"{name}.yml").read_text()) or {}
        derived.add((name, "fleet"))
        if host.get("claude_code_rc_enabled", rc_default):
            derived.add((name, "claude-rc"))
    assert derived == {
        ("daniel-box", "claude-rc"),
        ("daniel-box", "fleet"),
        ("daniel-server", "fleet"),
    }
    got = {
        (m.group(1), m.group(2))
        for rule in rules_named("ClaudeCgroupNotReporting")
        for m in re.finditer(
            r'origin="([^"]+)", kind="full", cgroup="([^"]+)"', rule["expr"]
        )
    }
    assert got == derived


def test_a_cgroup_pair_missing_from_the_rules_is_flagged():
    """The rejecting half: dropping a host's pair from the expr no longer equals the inventory."""
    got = {
        (m.group(1), m.group(2))
        for rule in rules_named("ClaudeCgroupNotReporting")
        for m in re.finditer(
            r'origin="([^"]+)", kind="full", cgroup="([^"]+)"',
            rule["expr"].replace('cgroup="claude-rc"', 'cgroup="renamed"'),
        )
    }
    assert ("daniel-box", "claude-rc") not in got
