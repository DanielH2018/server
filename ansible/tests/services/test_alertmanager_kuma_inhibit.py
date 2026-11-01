"""A dead Kuma pages once: `KumaDown` inhibits `ScrapeTargetDown{job="uptime-kuma"}`.

M6 batch B1's Cutover-1b makes `KumaDown` live (critical, 5m) beside the `ScrapeTargetDown`
rule that covers `job="uptime-kuma"` (warning, 10m). Both read `up{job="uptime-kuma"} == 0`, so
without the inhibit in `alertmanager-secret.yaml.j2` one dead Kuma pages twice. The inhibit's
`equal: [job, instance]` matches the one Kuma target, and keeps `KumaDown`'s `absent()` arm,
which carries no `instance`, from holding any scrape page.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest ansible/tests/services/test_alertmanager_kuma_inhibit.py
"""

from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_blackbox_inhibit import scrape_down_rule, target_alert
from test_alertmanager_host_check_inhibit import inhibited

HOST_CHECKS = ROLES / "k8s" / "observability" / "files" / "rules" / "host-checks.yaml"
KUMA_INSTANCE = "uptime-kuma.homelab.svc:3001"


def kuma_down_rule() -> dict:
    [group] = yaml_fast.safe_load(HOST_CHECKS.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r.get("alert") == "KumaDown"]
    return rule


def scrape_source(instance: str = KUMA_INSTANCE) -> dict[str, str]:
    """The labels the `up == 0` arm fires with: the series' labels plus the rule's."""
    return {
        "alertname": "KumaDown",
        "job": "uptime-kuma",
        "instance": instance,
        **kuma_down_rule()["labels"],
    }


def absent_source() -> dict[str, str]:
    """The labels the `absent()` arm fires with: the selector's literal plus the rule's."""
    return {
        "alertname": "KumaDown",
        "job": "uptime-kuma",
        **kuma_down_rule()["labels"],
    }


def test_a_dead_kuma_holds_its_own_scrape_target_page():
    assert inhibited(target_alert("uptime-kuma", KUMA_INSTANCE), [scrape_source()])


def test_a_scrape_page_for_another_instance_is_not_held():
    # RED if `equal` drops `instance`.
    other = "uptime-kuma.elsewhere.svc:3001"
    assert not inhibited(target_alert("uptime-kuma", other), [scrape_source()])


def test_another_jobs_scrape_target_page_is_not_held():
    # RED if the target matchers drop `job`: a dead node exporter would be silenced too.
    other = {**scrape_source(), "job": "node"}
    assert not inhibited(target_alert("node", KUMA_INSTANCE), [other])


def test_the_absent_arm_holds_no_scrape_target_page():
    assert not inhibited(target_alert("uptime-kuma", KUMA_INSTANCE), [absent_source()])


def test_no_other_alert_for_the_job_is_held():
    # RED if the target matchers drop `alertname`: ScrapeTargetFlapping (its own page) would be silenced.
    flapping = {
        **target_alert("uptime-kuma", KUMA_INSTANCE),
        "alertname": "ScrapeTargetFlapping",
    }
    assert not inhibited(flapping, [scrape_source()])


def test_source_and_target_rules_are_both_live():
    # Inhibition runs before routing, so a shadow source over a live target mutes the only page.
    source_shadow = "shadow" in kuma_down_rule()["labels"]
    target_shadow = "shadow" in scrape_down_rule("uptime-kuma")["labels"]
    assert (source_shadow, target_shadow) == (False, False)
