"""A dead blackbox_exporter pages once: `BlackboxExporterDown` inhibits `ScrapeTargetDown`.

M6 batch A3's Cutover-b makes `BlackboxExporterDown` live (critical, 5m) beside the
`ScrapeTargetDown` rule that covers `job="blackbox-ingress"` (warning, 10m). When the exporter
dies, every ingress target's scrape fails at once and both rules fire. The inhibit in
`alertmanager-secret.yaml.j2` keeps that to the one page. One dead target still pages through
`ScrapeTargetDown`, because `BlackboxExporterDown` fires only when every target reads 0.

This module evaluates the rendered `inhibit_rules` the way Alertmanager does, with the evaluator
`test_alertmanager_host_check_inhibit.py` uses. The plan is
docs/target-architecture/monitoring/m6-retire-kuma.md, "A3 and the handoffs it closes", task 4.

Run: uv run pytest ansible/tests/services/test_alertmanager_blackbox_inhibit.py
"""

import re

from lib import yaml_fast
from test_alertmanager_host_check_inhibit import _selects, inhibit_rules, inhibited
from test_alertmanager_routing import SCRAPE_RULES, ingress_set

SCRAPE_DOWN = "ScrapeTargetDown"
INGRESS_JOB = "blackbox-ingress"


def scrape_down_rule(job: str) -> dict:
    """The one `ScrapeTargetDown` rule whose job matcher names `job`, from the shipped rules."""
    [group] = yaml_fast.safe_load(SCRAPE_RULES.read_text())["groups"]
    named = re.compile(r'^up\{job(?:=~|=)"([^"]+)"\} == 0$')

    def jobs(rule: dict) -> list[str]:
        found = named.match(rule["expr"])
        return found.group(1).split("|") if found else []

    [rule] = [r for r in group["rules"] if r["alert"] == SCRAPE_DOWN and job in jobs(r)]
    return rule


def target_alert(
    job: str, instance: str = "https://homepage.local.example.com"
) -> dict:
    """The labels `ScrapeTargetDown` fires with for one dead target of `job`."""
    return {
        "alertname": SCRAPE_DOWN,
        "job": job,
        "instance": instance,
        **scrape_down_rule(job)["labels"],
    }


def exporter_alert() -> dict:
    """The labels `BlackboxExporterDown` fires with: its rule's labels, no `job` or `instance`."""
    return ingress_set("BlackboxExporterDown")


def exporter_inhibit() -> dict:
    [rule] = [r for r in inhibit_rules() if _selects(r, "source", exporter_alert())]
    return rule


def equal_problems(rule: dict, source: dict) -> list[str]:
    """The `equal` labels of `rule` that the source never carries, so it never matches."""
    return [label for label in rule.get("equal", []) if label not in source]


def test_a_dead_exporter_holds_the_scrape_target_page_for_its_job():
    for instance in (
        "https://homepage.local.example.com",
        "https://auth.local.example.com",
    ):
        assert inhibited(target_alert(INGRESS_JOB, instance), [exporter_alert()])


def test_one_dead_target_still_pages_while_the_exporter_rule_is_quiet():
    assert not inhibited(target_alert(INGRESS_JOB), [])


def test_another_jobs_scrape_target_page_is_not_held():
    # RED if the target matchers drop `job`: a dead node exporter would be silenced too.
    for job in ("node", "blackbox-extra", "prometheus"):
        assert not inhibited(target_alert(job), [exporter_alert()])


def test_the_exporter_alert_holds_no_other_alert_of_the_job():
    other = {**target_alert(INGRESS_JOB), "alertname": "IngressProbesMissing"}
    assert not inhibited(other, [exporter_alert()])


def test_the_equal_list_names_only_labels_the_exporter_alert_carries():
    assert equal_problems(exporter_inhibit(), exporter_alert()) == []


def test_an_equal_list_naming_job_is_flagged():
    # `BlackboxExporterDown` is a max() over the job, so `job` is absent on the source and an
    # `equal: [job]` would compare "" with "blackbox-ingress" and never match.
    bad = {**exporter_inhibit(), "equal": ["job"]}
    assert equal_problems(bad, exporter_alert()) == ["job"]


def test_source_and_target_rules_are_both_live():
    # Inhibition runs before routing, so a live source over a shadow target (or the reverse)
    # would mute a page that is the only one, or leave two.
    source_shadow = "shadow" in exporter_alert()
    target_shadow = "shadow" in scrape_down_rule(INGRESS_JOB)["labels"]
    assert (source_shadow, target_shadow) == (False, False)
