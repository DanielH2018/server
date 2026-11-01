"""The LAN must be exempt from UFW's SSH rate-limiter, and the exemption must sort above it.

WHY THIS IS A TEST AND NOT A COMMENT. `limit` REJECTs a source IP that opens 6 connections in
30 seconds on a rolling window, so retries hold the block open rather than riding it out.
Several Claude sessions reach the hosts over ssh at once; when that quorum crosses the
threshold, every session is locked out of the host for as long as anything keeps retrying.
The `DECIDED: the LAN is exempt` marker in `hosts/components/ssh-firewall/component.yml`
carries the reasoning; this pins the properties that reasoning depends on.

ufw matches rules in order and takes the first hit, so an `allow` BELOW the `limit` rule is
inert: present in `ufw status`, changing nothing. The component therefore declares the LAN
allow `position: first` with a `before:` relation on the limit rule, which reconcile's ufw arm
checks on every run, and both rules `keep`, so no revert deletes them.

Run: uv run pytest ansible/tests/setup/test_ssh_rate_limit_lan_exempt.py
"""

import re

from _helpers import REPO
from lib import yaml_fast

COMPONENT = REPO / "hosts" / "components" / "ssh-firewall" / "component.yml"
# The variable the exemption must be scoped to. Hardcoded rather than read from group_vars on
# purpose: the point is that the rule is scoped to the LAN at all.
_LAN_ALLOW = re.compile(r"allow from \{\{ lan_subnet \}\} to any port 22 proto tcp")
_LIMIT = re.compile(r"limit 22(/tcp)?")


def ssh_limiter_problems(rules: list[dict]) -> list[str]:
    """Every reason these `ufw.rules` would still rate-limit SSH from the LAN; empty means clean."""
    allow = next((r for r in rules if _LAN_ALLOW.fullmatch(str(r.get("spec")))), None)
    limit = next((r for r in rules if _LIMIT.fullmatch(str(r.get("spec")))), None)
    if allow is None:
        return [
            "no ufw `allow` rule on port 22 scoped to {{ lan_subnet }}. Without it the "
            "`limit` rule rate-limits LAN hosts, and concurrent agent sessions livelock ssh."
        ]
    if limit is None:
        return [
            "no ufw `limit` rule on port 22 remains. If the limiter was removed entirely this "
            "guard no longer pins anything -- delete it or rewrite it deliberately."
        ]
    problems = []
    if allow.get("position") != "first":
        problems.append(
            "the LAN `allow` rule is not `position: first`. Every host already carries the "
            "`limit` rule, so an appended allow lands BELOW it and is inert."
        )
    if allow.get("before") != limit.get("id"):
        problems.append(
            "the LAN `allow` rule declares no `before:` relation on the `limit` rule, so "
            "reconcile never checks the order it depends on."
        )
    if not (allow.get("keep") and limit.get("keep")):
        problems.append(
            "the SSH rules are not both `keep`, so a revert could delete one and lock a "
            "remote host out."
        )
    return problems


def _rules() -> list[dict]:
    return yaml_fast.safe_load(COMPONENT.read_text())["ufw"]["rules"]


def test_the_ssh_firewall_component_exempts_the_lan():
    assert ssh_limiter_problems(_rules()) == []


_ALLOW = {
    "id": "lan-ssh",
    "spec": "allow from {{ lan_subnet }} to any port 22 proto tcp",
    "keep": True,
    "position": "first",
    "before": "ssh-limit",
}
_LIMIT_RULE = {"id": "ssh-limit", "spec": "limit 22/tcp", "keep": True}


def test_the_fixture_pair_is_itself_clean():
    """Guards the rejecting tests below: they only prove something if the base fixture passes."""
    assert ssh_limiter_problems([_ALLOW, _LIMIT_RULE]) == []


def test_a_missing_lan_allow_is_flagged():
    problems = ssh_limiter_problems([_LIMIT_RULE])
    assert any("no ufw `allow` rule" in p for p in problems), problems


def test_an_allow_scoped_to_something_other_than_the_lan_is_flagged():
    narrowed = _ALLOW | {"spec": "allow from 192.0.2.15 to any port 22 proto tcp"}
    problems = ssh_limiter_problems([narrowed, _LIMIT_RULE])
    assert any("no ufw `allow` rule" in p for p in problems), problems


def test_an_allow_that_is_appended_is_flagged():
    appended = {k: v for k, v in _ALLOW.items() if k != "position"}
    problems = ssh_limiter_problems([appended, _LIMIT_RULE])
    assert any("not `position: first`" in p for p in problems), problems


def test_an_allow_with_no_order_relation_is_flagged():
    unordered = {k: v for k, v in _ALLOW.items() if k != "before"}
    problems = ssh_limiter_problems([unordered, _LIMIT_RULE])
    assert any("no `before:` relation" in p for p in problems), problems


def test_a_deletable_ssh_rule_is_flagged():
    problems = ssh_limiter_problems([_ALLOW, _LIMIT_RULE | {"keep": False}])
    assert any("not both `keep`" in p for p in problems), problems
