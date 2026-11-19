"""Tests for gen_host_rules, which renders observability's host-plane rule groups from tables.

The committed `files/rules/host-checks.yaml` and `hosts.yaml` must be what the generator renders,
and a host added to the host table must gain every copy its rules' selectors take, with no
table edit. Each refusal is paired with the input it accepts.

Run: uv run pytest scripts/dev/tests/test_gen_host_rules.py
"""

import re
import shutil

import pytest

import gen_host_rules as g
from lib import yaml_fast

# Non-vacuity: the hosts the inventory must keep declaring for the host table to mean much.
FLEET = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
# Non-vacuity: the k3s nodes that pin their address on a wired link with a WiFi fallback.
WIRED_NODES = frozenset({"daniel-box", "daniel-server"})
NEW_HOST = "daniel-new"


def _copies(text: str) -> dict[str, list[dict]]:
    """Each alert's rendered copies in a rule file's text."""
    copies = {}
    for group in yaml_fast.safe_load(text)["groups"]:
        for rule in group["rules"]:
            copies.setdefault(rule["alert"], []).append(rule)
    return copies


def _services(copies: dict[str, list[dict]], alert: str) -> set[str]:
    return {rule["labels"]["service"] for rule in copies[alert]}


@pytest.fixture(scope="module")
def facts() -> dict[str, dict]:
    return g.host_facts(g.expected_hosts())


def test_the_committed_rule_files_are_the_render_of_their_tables_is_clean():
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
    assert g.main(["--check"]) == 0


def test_a_rule_file_edited_by_hand_is_flagged(tmp_path, capsys):
    role = tmp_path / "observability"
    for part in ("defaults", "files/rule-tables", "files/rules"):
        shutil.copytree(g.ROLE / part, role / part)
    edited = role / "files" / "rules" / "hosts.yaml"
    edited.write_text(edited.read_text().replace("for: 15m", "for: 20m", 1))
    assert g.main(["--check"], role=role) == 1
    assert "hosts.yaml differs from its table" in capsys.readouterr().err


def test_each_shipped_selector_reaches_the_hosts_it_must(facts):
    copies = _copies(g.render("host-checks", facts=facts))
    assert FLEET <= set(g.expected_hosts())
    assert _services(copies, "HostReconcileFailed") == set(g.expected_hosts())
    assert _services(copies, "HostUpdateStale") == set(g.expected_hosts())
    assert _services(copies, "WiredLinkDown") == WIRED_NODES
    assert _services(copies, "AideCheckFailed") == {"daniel-box", "daniel-server"}


def test_a_new_host_gains_the_copies_its_selectors_take_with_no_table_edit(facts):
    new = {
        "job": "node",
        "limit_seconds": 7200,
        "components": {"host-maintenance"},
        "host_vars": {"k3s_node_static_link": "eth9"},
    }
    copies = _copies(g.render("host-checks", facts={**facts, NEW_HOST: new}))
    for alert in ("HostReconcileFailed", "AptAutoremoveFailed", "IscsiSessionStale"):
        assert NEW_HOST in _services(copies, alert), alert
    assert NEW_HOST not in _services(copies, "AideCheckFailed")
    [stale] = [
        r for r in copies["HostUpdateStale"] if r["labels"]["service"] == NEW_HOST
    ]
    assert "> 7200" in stale["expr"]
    assert stale["annotations"]["summary"].endswith("for 2 hours.")
    [wired] = [r for r in copies["WiredLinkDown"] if r["labels"]["service"] == NEW_HOST]
    assert 'device="eth9",origin="daniel-new"' in wired["expr"]


def test_a_per_host_override_reaches_its_host_alone(facts):
    copies = _copies(g.render("hosts", facts=facts))
    arms = {
        rule["labels"]["service"]: "pci0000:00_0000:00:18_3" in rule["expr"]
        for rule in copies["HostTemperatureHot"]
    }
    assert arms == {"daniel-box": True, "daniel-server": False, "daniel-pi": False}


FACTS = {
    "a": {"job": "node", "limit_seconds": 60, "components": {"x"}, "host_vars": {}},
    "b": {
        "job": "node-pi",
        "limit_seconds": None,
        "components": set(),
        "host_vars": {},
    },
}


def _rule(**extra) -> dict:
    return {"alert": "A", "expr": 'up{origin="${host}"} == 0', **extra}


@pytest.mark.parametrize(
    ("selector", "hosts"),
    [
        ("all", ["a", "b"]),
        (["b"], ["b"]),
        ({"job": "node-pi"}, ["b"]),
        ({"component": "x"}, ["a"]),
    ],
)
def test_a_selector_picks_its_hosts_is_clean(selector, hosts):
    rendered = g.expand([_rule(hosts=selector)], FACTS)
    assert [rule["expr"] for rule in rendered] == [
        f'up{{origin="{h}"}} == 0' for h in hosts
    ]


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (_rule(hosts=["c"]), "['c'] are not in observability_expected_hosts"),
        (_rule(hosts={"role": "x"}), "is no selector"),
        (_rule(hosts="all", summary="${unknown}"), "${unknown} has no value"),
        (_rule(hosts=["b"], summary="${limit}"), "${limit} has no value"),
        (_rule(), "${host} has no value"),
        (_rule(hosts=["a"], per_host={"b": {}}), "per_host names ['b']"),
    ],
)
def test_a_rule_that_cannot_render_is_flagged(rule, message):
    with pytest.raises(g.TableError, match=re.escape(message)):
        g.expand([rule], FACTS)


def test_every_inventory_and_armed_host_is_in_the_host_table_is_clean():
    inventory = g.host_names()
    assert FLEET <= set(inventory)
    armed = g.armed_hosts(inventory)
    assert FLEET <= armed
    assert g.untabled_hosts(g.expected_hosts(), inventory, armed) == []


def test_a_host_missing_from_the_host_table_is_flagged():
    table = {"daniel-box": {}}
    assert g.untabled_hosts(table, ("daniel-box", "daniel-new"), {"daniel-armed"}) == [
        "daniel-new is an inventory host and observability_expected_hosts does not list it",
        "daniel-armed enables homelab-updater.timer and observability_expected_hosts does not "
        "list it",
    ]
