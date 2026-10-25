"""A per-check `*NotReporting` alert inhibits `HostCheckOverdue` for the same check and origin.

`HostCheckOverdue` (`host-checks.yaml`, one rule per host) compares each check's timestamp with
the deadline the check declares, so it fires beside every per-check NotReporting rule's stale
arm. An Alertmanager inhibit keyed on `check` and `origin` suppresses that duplicate, which
lets the catch-all page alone for a producer with no pair (#6186). This module evaluates the
rendered `inhibit_rules` the way Alertmanager does: a target alert is muted while a source
alert fires whose `equal` labels match it, a label both lack counting as equal.

Run: uv run pytest ansible/tests/services/test_alertmanager_host_check_inhibit.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import ROLES
from _k8s_render import render_role_template
from test_alertmanager_routing import BLACKHOLE, reached, route_tree

HOST_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "host-checks.yaml"
OVERDUE = "HostCheckOverdue"
MATCHER = re.compile(r'^(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"$')
# The stale arm of a NotReporting rule names its check and origin as selector literals.
STALE_ARM = re.compile(
    r'homelab_check_last_run_timestamp_seconds\{check="([^"]+)",origin="([^"]+)"\}'
)


def host_rules() -> list[dict]:
    [group] = yaml_fast.safe_load(HOST_RULES.read_text())["groups"]
    return group["rules"]


def inhibit_rules() -> list[dict]:
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    config = yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])
    return config.get("inhibit_rules") or []


def _matcher(matcher: str, labels: dict[str, str]) -> bool:
    found = MATCHER.match(matcher)
    if found is None:
        raise AssertionError(f"the evaluator cannot read matcher {matcher!r}; teach it")
    name, op, value = found.groups()
    actual = labels.get(name, "")
    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "=~":
        return re.fullmatch(value, actual) is not None
    return re.fullmatch(value, actual) is None


def _selects(rule: dict, side: str, labels: dict[str, str]) -> bool:
    """Whether `labels` matches the `side` (`source` or `target`) of an inhibit rule, in
    either the `*_matchers` form or the older `*_match`/`*_match_re` maps."""
    matchers = rule.get(f"{side}_matchers", [])
    exact = rule.get(f"{side}_match", {})
    regex = rule.get(f"{side}_match_re", {})
    if not (matchers or exact or regex):
        return False
    return (
        all(_matcher(m, labels) for m in matchers)
        and all(labels.get(k, "") == v for k, v in exact.items())
        and all(re.fullmatch(v, labels.get(k, "")) for k, v in regex.items())
    )


def inhibited(target: dict[str, str], firing: list[dict[str, str]]) -> bool:
    """Whether Alertmanager mutes `target` while every alert in `firing` fires."""
    for rule in inhibit_rules():
        if not _selects(rule, "target", target):
            continue
        for source in firing:
            if source is target or not _selects(rule, "source", source):
                continue
            if all(
                source.get(k, "") == target.get(k, "") for k in rule.get("equal", [])
            ):
                return True
    return False


def overdue_rules() -> list[dict]:
    rules = [r for r in host_rules() if r["alert"] == OVERDUE]
    assert len(rules) == 3, [r["labels"]["service"] for r in rules]
    return rules


def overdue_alert(check: str, origin: str) -> dict[str, str]:
    """The labels HostCheckOverdue fires with for `check` on `origin`: its rule's labels plus
    the series labels the `> on (check, origin)` comparison keeps from the timestamp."""
    [rule] = [r for r in overdue_rules() if r["labels"]["service"] == origin]
    return {
        "alertname": OVERDUE,
        **rule["labels"],
        "check": check,
        "origin": origin,
        "job": "node-pi" if origin == "daniel-pi" else "node",
        "instance": f"{origin}:9100",
    }


def not_reporting_pairs() -> dict[str, dict[str, str]]:
    """Each per-check NotReporting alert as its stale arm fires it, keyed `<alert>/<origin>`."""
    pairs = {}
    for rule in host_rules():
        if not rule["alert"].endswith("NotReporting"):
            continue
        found = STALE_ARM.search(rule["expr"])
        assert found, rule["alert"]
        check, origin = found.groups()
        pairs[f"{rule['alert']}/{origin}"] = {
            "alertname": rule["alert"],
            **rule["labels"],
            "check": check,
            "origin": origin,
            "job": "node-pi" if origin == "daniel-pi" else "node",
            "instance": f"{origin}:9100",
        }
    return pairs


PAIRS = not_reporting_pairs()


def test_alertmanager_inhibits_host_check_overdue_on_check_and_origin():
    targets = [
        rule
        for rule in inhibit_rules()
        if _selects(rule, "target", overdue_alert("aide-check", "daniel-box"))
    ]
    assert [sorted(rule.get("equal", [])) for rule in targets] == [["check", "origin"]]
    [rule] = targets
    assert _selects(rule, "source", PAIRS["AideCheckNotReporting/daniel-box"])


def test_host_check_overdue_carries_no_shadow_label():
    assert [
        r["labels"]["service"] for r in overdue_rules() if "shadow" in r["labels"]
    ] == []


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_a_paired_not_reporting_alert_inhibits_the_duplicate_overdue(name):
    # Non-vacuity for `PAIRS`: the cases run on every host's HostCheckOverdue.
    assert {key.split("/")[1] for key in PAIRS} == {
        "daniel-box",
        "daniel-server",
        "daniel-pi",
    }
    source = PAIRS[name]
    target = overdue_alert(source["check"], source["origin"])
    assert inhibited(target, [source, target])


def test_an_unpaired_check_overdue_pages_beside_another_checks_not_reporting():
    source = PAIRS["AideCheckNotReporting/daniel-box"]
    target = overdue_alert("a-producer-with-no-pair", "daniel-box")
    assert not inhibited(target, [source, target])
    assert [r["receiver"] for r in reached(route_tree(), target)] == ["discord"]


def test_the_same_check_overdue_on_another_origin_still_pages():
    source = PAIRS["AideCheckNotReporting/daniel-box"]
    target = overdue_alert("aide-check", "daniel-server")
    assert not inhibited(target, [source, target])
    assert BLACKHOLE not in [r["receiver"] for r in reached(route_tree(), target)]
