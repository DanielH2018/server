"""The CronJob half of `pod-producers.yaml` names the objects the roles render, at the age limit
their schedule implies.

The rule file is plain YAML, so each ConfigMap name, CronJob name and age limit is a literal that
mirrors a role default or a rendered CronJob (M1 convention 1). A role that renamed its verdict
ConfigMap, or retuned its health schedule, would leave the shadow rule reading an object that no
longer exists and staying quiet. M6 batch B6b.

Run: uv run pytest ansible/tests/services/test_pod_producer_rule_literals.py
"""

import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "pod-producers.yaml"
# The rule names per role, and the Kuma tile each ports, so a tile rename is a deliberate edit.
ROLES = {
    "configarr": ("ConfigarrSyncFailed", "ConfigarrHealthNotReporting"),
    "janitorr": ("JanitorrErrorsFound", "JanitorrHealthNotReporting"),
}
PERIODS = 3  # the deadline: one missed run is slow, two is hung, the third is the first not noise


def rules_by_name(text: str) -> dict[str, dict]:
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def rendered(role: str, template: str) -> list[dict]:
    return [
        d for d in yaml_fast.safe_load_all(render_role_template(role, template)) if d
    ]


def period_seconds(schedule: str) -> int:
    """Seconds between runs of a `*/N * * * *` schedule, the only shape these CronJobs use."""
    minutes = schedule.split()[0]
    assert minutes.startswith("*/") and schedule.split()[1:] == ["*"] * 4, schedule
    return int(minutes.removeprefix("*/")) * 60


def literal_problems(rules: dict[str, dict]) -> list[str]:
    """Each rule fragment that disagrees with the CronJob and ConfigMap the role renders."""
    problems = []
    for role, (failed, silent) in ROLES.items():
        [cronjob] = rendered(role, "health-cronjob.yaml.j2")
        [verdict_cm] = rendered(role, "health-verdict-configmap.yaml.j2")
        name = cronjob["metadata"]["name"]
        configmap = verdict_cm["metadata"]["name"]
        env = {
            e["name"]: e["value"]
            for e in cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
                "containers"
            ][0]["env"]
            if "value" in e
        }
        if env.get(f"{role.upper()}_VERDICT_CONFIGMAP") != configmap:
            problems.append(
                f"{role}: the reader writes a ConfigMap the role does not render"
            )
        limit = PERIODS * period_seconds(cronjob["spec"]["schedule"])
        fragments = {
            failed: [
                f'configmap="{configmap}"',
                'annotation_homelab_check_verdict="down"',
            ],
            silent: [f'cronjob="{name}"', f"> {limit}", f"[{limit // 60}m]"],
        }
        for alert, wanted in fragments.items():
            expr = " ".join(str(rules[alert]["expr"]).split())
            problems += [f"{alert}: expr lacks `{w}`" for w in wanted if w not in expr]
    return problems


def test_the_cronjob_rules_name_what_the_roles_render():
    assert literal_problems(rules_by_name(RULES.read_text())) == []


def test_the_rules_find_both_named_roles():
    # The named members: a rule table that lost a role would pass an empty loop.
    assert set(ROLES) == {"configarr", "janitorr"}
    assert {a for pair in ROLES.values() for a in pair} <= set(
        rules_by_name(RULES.read_text())
    )


@pytest.mark.parametrize(
    ("old", "new", "alert"),
    [
        (
            'configmap="configarr-health-verdict"',
            'configmap="configarr-verdict"',
            "ConfigarrSyncFailed",
        ),
        ("> 1800", "> 3600", "JanitorrHealthNotReporting"),
        (
            'cronjob="janitorr-health"',
            'cronjob="janitorr-healthcheck"',
            "JanitorrHealthNotReporting",
        ),
    ],
)
def test_a_renamed_object_or_retuned_limit_is_flagged(old, new, alert):
    text = RULES.read_text()
    # Replace only inside the named rule, so a literal shared by two rules cannot hide it.
    head, _, tail = text.partition(f"- alert: {alert}\n")
    body, sep, rest = tail.partition("      - alert:")
    assert old in body, (alert, old)
    mutated = head + f"- alert: {alert}\n" + body.replace(old, new) + sep + rest
    assert any(p.startswith(alert) for p in literal_problems(rules_by_name(mutated))), (
        literal_problems(rules_by_name(mutated))
    )
