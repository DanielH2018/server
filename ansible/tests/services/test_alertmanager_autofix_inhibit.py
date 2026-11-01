"""A dead autofix-bridge exporter pages once: `AutofixBridgeDown` inhibits `ScrapeTargetDown`.

M6 batch B7's Cutover-1b makes `AutofixBridgeDown` live (critical, 10m) beside the
`ScrapeTargetDown` rule that covers `job="autofix-bridge"` (warning, 10m). When the exporter
dies both rules read the same `up == 0`. The inhibit in `alertmanager-secret.yaml.j2` keeps that
to the one page (docs/target-architecture/monitoring/m6-retire-kuma.md, "A3 and the handoffs it
closes", task 6). It has no `equal` list: the job has one target, and the rule's `absent()` arm
carries no `instance`.

Alertmanager inhibits before it routes, so the inhibit reads shadow alerts too. A shadow source
over a live target would mute the only page, which is why the last two tests pin both rules live.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest ansible/tests/services/test_alertmanager_autofix_inhibit.py
"""

from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_blackbox_inhibit import scrape_down_rule, target_alert
from test_alertmanager_host_check_inhibit import inhibited
from test_alertmanager_routing import BLACKHOLE, reached, route_tree

PODS_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "pods.yaml"
JOB = "autofix-bridge"


def down_labels() -> dict[str, str]:
    """The labels `AutofixBridgeDown` fires with: its rule's labels plus the job literal."""
    [group] = yaml_fast.safe_load(PODS_RULES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == "AutofixBridgeDown"]
    return {"alertname": "AutofixBridgeDown", "job": JOB, **rule["labels"]}


def test_a_dead_exporter_holds_its_own_scrape_target_page():
    assert inhibited(target_alert(JOB, "autofix-bridge:9182"), [down_labels()])


def test_a_scrape_target_page_alone_is_not_held():
    assert not inhibited(target_alert(JOB, "autofix-bridge:9182"), [])


def test_another_jobs_scrape_target_page_is_not_held():
    # RED if the target matchers drop `job`: a dead node exporter would be silenced too.
    for job in ("node", "kube-state-metrics", "monitor-bridge"):
        assert not inhibited(target_alert(job, "x:1"), [down_labels()])


def test_the_down_alert_holds_no_other_alert_of_the_job():
    # RED if the target matchers drop `alertname`: the flap arm would be silenced too.
    other = {
        **target_alert(JOB, "autofix-bridge:9182"),
        "alertname": "ScrapeTargetFlapping",
    }
    assert not inhibited(other, [down_labels()])


def test_source_and_target_rules_are_both_live():
    assert "shadow" not in down_labels()
    assert "shadow" not in scrape_down_rule(JOB)["labels"]


def test_a_shadow_source_would_mute_the_page_and_page_nobody():
    # The rejecting half of the pair above: with `shadow` back on the source, the inhibitor still
    # holds the target, and the walk sends the source to the blackhole.
    shadowed = {**down_labels(), "shadow": "true"}
    assert inhibited(target_alert(JOB, "autofix-bridge:9182"), [shadowed])
    assert [r["receiver"] for r in reached(route_tree(), shadowed)] == [BLACKHOLE]
