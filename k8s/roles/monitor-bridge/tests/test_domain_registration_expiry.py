"""A days-to-expiry metric for the domain's registration, and the rules that alert on it (#8519).

The rules are found by what they watch, not by a name the fix has yet to choose: a shipped alert
whose name or expression mentions the domain registration, RDAP or whois. The pytest shards
install no promtool, so the firing tests read the rules' promtool cases, which the
validate-alert-rules hook runs.

Run: uv run pytest k8s/roles/monitor-bridge/tests/test_domain_registration_expiry.py
"""

import re
from _helpers import K8S_ROLES, REPO, load_yaml
from lib.proc_testing import run

OBSERVABILITY = K8S_ROLES / "observability"
MONITOR_BRIDGE = K8S_ROLES / "monitor-bridge"
SUBJECT = re.compile(r"domain|registration|rdap|whois", re.IGNORECASE)
WARN_DAYS = 45
PAGE_DAYS = 14
DAY_S = 86400


def _shipped_rules() -> list[tuple[str, dict]]:
    groups = load_yaml(OBSERVABILITY / "defaults" / "main.yml")[
        "observability_rule_groups"
    ]
    return [
        (name, rule)
        for name in groups
        for group in load_yaml(OBSERVABILITY / "files" / "rules" / f"{name}.yaml")[
            "groups"
        ]
        for rule in group["rules"]
        if "alert" in rule
    ]


def _registration_rules() -> list[tuple[str, dict]]:
    return [
        (name, rule)
        for name, rule in _shipped_rules()
        if SUBJECT.search(rule["alert"]) or SUBJECT.search(rule["expr"])
    ]


def _rule_at(days: int) -> tuple[str, dict]:
    threshold = re.compile(rf"<=?\s*{days}(?:\.0+)?\b")
    found = [(n, r) for n, r in _registration_rules() if threshold.search(r["expr"])]
    assert found, (
        f"no shipped alert on the domain registration compares its days-to-expiry "
        f"with {days}"
    )
    return found[0]


def _metrics(expr: str) -> set[str]:
    return set(re.findall(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b(?!\s*\()", expr))


def test_a_rule_warns_45_days_before_the_registration_expires():
    _, rule = _rule_at(WARN_DAYS)
    assert rule.get("labels", {}).get("severity") == "warning"


def test_a_rule_pages_14_days_before_the_registration_expires():
    _, rule = _rule_at(PAGE_DAYS)
    assert rule.get("labels", {}).get("severity") == "critical"


def test_monitor_bridge_exports_the_metric_both_rules_read():
    warn_metrics = _metrics(_rule_at(WARN_DAYS)[1]["expr"])
    page_metrics = _metrics(_rule_at(PAGE_DAYS)[1]["expr"])
    shared = warn_metrics & page_metrics
    assert shared, "the warning and the page read no metric in common"
    tracked = run(
        ["git", "ls-files", str(MONITOR_BRIDGE / "files")],
        cwd=REPO,
        check=True,
    ).stdout.split()
    code = "\n".join((REPO / p).read_text() for p in tracked if p.endswith(".py"))
    emitted = {m for m in shared if m in code}
    assert emitted, f"no monitor-bridge module emits {sorted(shared)}"


def test_each_registration_rule_has_a_promtool_case_that_fires_it():
    for days in (WARN_DAYS, PAGE_DAYS):
        group, rule = _rule_at(days)
        path = OBSERVABILITY / "tests" / "rules" / f"{group}.test.yaml"
        assert path.is_file(), f"{rule['alert']} has no promtool cases: {path}"
        firing = [
            check
            for case in load_yaml(path)["tests"]
            for check in case.get("alert_rule_test", [])
            if check["alertname"] == rule["alert"] and check.get("exp_alerts")
        ]
        assert firing, f"no promtool case expects {rule['alert']} to fire"


def test_rdap_is_read_once_a_day():
    env = load_yaml(MONITOR_BRIDGE / "templates" / "config" / "env.yaml.j2")
    intervals = {
        key: value
        for key, value in env.items()
        if re.search(r"DOMAIN|RDAP|REGISTRATION", key) and key.endswith("INTERVAL_S")
    }
    assert intervals, "no monitor-bridge env key sets the RDAP probe interval"
    assert set(intervals.values()) == {str(DAY_S)}
