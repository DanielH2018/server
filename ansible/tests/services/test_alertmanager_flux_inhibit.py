"""A dead Flux controller pages once: `FluxControllerDown` inhibits `ScrapeTargetDown{job="flux"}`.

Both rules read the same `up{job="flux"} == 0`. `FluxControllerDown` pages it critical after 5
minutes and `ScrapeTargetDown` pages it as a warning after 10, so without the inhibit in
`alertmanager-secret.yaml.j2` one outage pages twice (#6654). The inhibit's `equal: [job,
instance]` keeps another controller's scrape page live, and keeps the rule's `absent()` arm,
which carries no `instance`, from holding any scrape page.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest ansible/tests/services/test_alertmanager_flux_inhibit.py
"""

from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_blackbox_inhibit import scrape_down_rule, target_alert
from test_alertmanager_host_check_inhibit import inhibited

FLUX_RULES = ROLES / "k8s" / "observability" / "files" / "rules" / "flux.yaml"
SOURCE = "kustomize-controller"
SOURCE_INSTANCE = "10.42.1.207:8080"
OTHER_INSTANCE = "10.42.1.206:8080"


def controller_down_rules() -> dict[str, dict]:
    """The two `FluxControllerDown` rules, keyed `up` for the scrape arm and `absent`."""
    [group] = yaml_fast.safe_load(FLUX_RULES.read_text())["groups"]
    found = [r for r in group["rules"] if r.get("alert") == "FluxControllerDown"]
    keyed = {"up" if r["expr"] == 'up{job="flux"} == 0' else "absent": r for r in found}
    assert set(keyed) == {"up", "absent"}, [r["expr"] for r in found]
    return keyed


def scrape_source(instance: str = SOURCE_INSTANCE) -> dict[str, str]:
    """The labels the `up == 0` arm fires with: the series' labels plus the rule's."""
    return {
        "alertname": "FluxControllerDown",
        "job": "flux",
        "app": SOURCE,
        "instance": instance,
        "pod": f"{SOURCE}-5d94ff85cc-bfhww",
        **controller_down_rules()["up"]["labels"],
    }


def absent_source() -> dict[str, str]:
    """The labels the `absent()` arm fires with: the selector's literals plus the rule's."""
    return {
        "alertname": "FluxControllerDown",
        "job": "flux",
        "app": SOURCE,
        **controller_down_rules()["absent"]["labels"],
    }


def test_a_dead_controller_holds_its_own_scrape_target_page():
    assert inhibited(target_alert("flux", SOURCE_INSTANCE), [scrape_source()])


def test_another_controllers_scrape_target_page_is_not_held():
    # RED if `equal` drops `instance`: one dead controller would mute the other two.
    assert not inhibited(target_alert("flux", OTHER_INSTANCE), [scrape_source()])


def test_another_jobs_scrape_target_page_is_not_held():
    # RED if the target matchers drop `job`.
    other = {**scrape_source(), "job": "node"}
    assert not inhibited(target_alert("node", SOURCE_INSTANCE), [other])


def test_the_absent_arm_holds_no_scrape_target_page():
    assert not inhibited(target_alert("flux", SOURCE_INSTANCE), [absent_source()])


def test_source_and_target_rules_are_both_live():
    # Inhibition runs before routing, so a shadow source over a live target mutes the only page.
    source_shadow = "shadow" in controller_down_rules()["up"]["labels"]
    target_shadow = "shadow" in scrape_down_rule("flux")["labels"]
    assert (source_shadow, target_shadow) == (False, False)
