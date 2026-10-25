"""`pods.yaml` names the objects the B7 roles render, at the limit their schedule implies.

The rule file is plain YAML, so each `check` label, Deployment name, ConfigMap name, CronJob name
and age limit is a literal that mirrors a role default, a rendered manifest or a constant in a
script (M1 convention 1). A role that renamed its verdict ConfigMap, a Deployment that changed
name, or a retuned schedule would leave a shadow rule reading an object that no longer exists
and staying quiet. M6 batch B7. `test_pod_producer_rule_literals.py` does the same for B6b's
`pod-producers.yaml`.

Run: uv run pytest ansible/tests/services/test_pods_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "pods.yaml"
AUTOFIX = K8S_ROLES / "autofix-bridge" / "files" / "autofix.py"
PERIODS = (
    3  # one missed run is slow, two is hung, the third is the first that is not noise
)


def rules_by_name(text: str) -> dict[str, dict]:
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def rendered(role: str, template: str) -> list[dict]:
    return [
        d for d in yaml_fast.safe_load_all(render_role_template(role, template)) if d
    ]


def daily_period_seconds(schedule: str) -> int:
    """Seconds between runs of a `M H * * *` schedule, the only shape pi-peer-backup uses."""
    minute, hour, *rest = schedule.split()
    assert minute.isdigit() and hour.isdigit() and rest == ["*"] * 3, schedule
    return 86400


def expr(rules: dict[str, dict], alert: str) -> str:
    return " ".join(str(rules[alert]["expr"]).split())


def literal_problems(rules: dict[str, dict]) -> list[str]:
    """Each rule fragment that disagrees with the role, manifest or script it mirrors."""
    problems = []

    def need(alert: str, fragments: list[str]) -> None:
        problems.extend(
            f"{alert}: expr lacks `{f}`"
            for f in fragments
            if f not in expr(rules, alert)
        )

    # autofix-bridge: the `check` label autofix.py exports, and the scrape job the entry renders.
    [check] = re.findall(r'^CHECK = "([a-z-]+)"', AUTOFIX.read_text(), flags=re.M)
    for alert in ("ArrAutoBlockFailing",):
        need(alert, [f'check="{check}"', 'job="autofix-bridge"'])
    for alert in ("AutofixBridgeDown", "AutofixBridgeStale"):
        need(alert, ['job="autofix-bridge"'])

    # cloudflare-ddns: both Deployments, by the names the templates render.
    deployments = sorted(
        d["metadata"]["name"]
        for template in ("deployment-direct.yaml.j2", "deployment-proxied.yaml.j2")
        for d in rendered("cloudflare-ddns", template)
        if d["kind"] == "Deployment"
    )
    need("CloudflareDdnsUnavailable", [f'deployment="{name}"' for name in deployments])
    if not re.search(
        r'deployment=~"cloudflare-ddns-\(direct\|proxied\)"',
        expr(rules, "CloudflareDdnsUnavailable"),
    ):
        problems.append(
            "CloudflareDdnsUnavailable: the regex arm names neither Deployment"
        )

    # pi-peer-backup: the verdict ConfigMap, the CronJob and its age limit.
    [cronjob] = [
        d
        for d in rendered("pi-peer-backup", "cronjob.yaml.j2")
        if d["kind"] == "CronJob"
    ]
    [configmap] = rendered("pi-peer-backup", "verdict-configmap.yaml.j2")
    env = {
        e["name"]: e["value"]
        for e in cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
        if "value" in e
    }
    name = configmap["metadata"]["name"]
    if env.get("VERDICT_CONFIGMAP") != name:
        problems.append(
            "pi-peer-backup: the script writes a ConfigMap the role does not render"
        )
    limit = PERIODS * daily_period_seconds(cronjob["spec"]["schedule"])
    need(
        "PiPeerBackupFailed",
        [f'configmap="{name}"', 'annotation_homelab_check_verdict="down"'],
    )
    need(
        "PiPeerBackupNotReporting",
        [
            f'cronjob="{cronjob["metadata"]["name"]}"',
            f"> {limit}",
            f"[{limit // 3600}h]",
        ],
    )
    return problems


def test_the_rules_name_what_the_roles_and_the_script_render():
    assert literal_problems(rules_by_name(RULES.read_text())) == []


LOKI_RULES = K8S_ROLES / "loki-homelab" / "files" / "rules" / "homelab-logs.yaml"
DDNS_LOG_RULES = {
    "CloudflareDdnsDirectUpdateSilent": "cloudflare-ddns-direct",
    "CloudflareDdnsProxiedUpdateSilent": "cloudflare-ddns-proxied",
}


def loki_ddns_rules(text: str) -> dict[str, dict]:
    [group] = yaml_fast.safe_load(text)["groups"]
    return {r["alert"]: r for r in group["rules"] if r["alert"] in DDNS_LOG_RULES}


def log_rule_problems(rules: dict[str, dict], deployments: set[str]) -> list[str]:
    """Each cloudflare-ddns LogQL rule that is not shadow or names a pod no Deployment owns."""
    problems = [f"{a}: missing" for a in DDNS_LOG_RULES if a not in rules]
    for alert, rule in rules.items():
        if rule["labels"].get("shadow") != "true":
            problems.append(f"{alert}: not shadow")
        pods = re.findall(r'pod=~"([^"]*)"', rule["expr"])
        if (
            pods != [f"{DDNS_LOG_RULES[alert]}-.*"]
            or DDNS_LOG_RULES[alert] not in deployments
        ):
            problems.append(
                f"{alert}: pod regex {pods} starts no rendered Deployment name"
            )
    return problems


def test_the_cloudflare_ddns_log_rules_are_shadow_and_name_a_rendered_deployment():
    deployments = {
        d["metadata"]["name"]
        for template in ("deployment-direct.yaml.j2", "deployment-proxied.yaml.j2")
        for d in rendered("cloudflare-ddns", template)
        if d["kind"] == "Deployment"
    }
    assert deployments == set(DDNS_LOG_RULES.values())
    rules = loki_ddns_rules(LOKI_RULES.read_text())
    assert log_rule_problems(rules, deployments) == []
    # The red proof: a renamed Deployment leaves the rule selecting no stream.
    assert log_rule_problems(rules, {"cloudflare-ddns-main"}) != []


def test_the_rules_find_every_named_alert():
    # The named members: a rule table that lost an alert would pass an empty loop.
    assert set(rules_by_name(RULES.read_text())) == {
        "ArrAutoBlockFailing",
        "AutofixBridgeDown",
        "AutofixBridgeStale",
        "CloudflareDdnsUnavailable",
        "PiPeerBackupFailed",
        "PiPeerBackupNotReporting",
    }


@pytest.mark.parametrize(
    ("old", "new", "alert"),
    [
        ('check="arr-autoblock"', 'check="arr-auto-block"', "ArrAutoBlockFailing"),
        (
            'configmap="pi-peer-backup-verdict"',
            'configmap="pi-peer-backup-verdicts"',
            "PiPeerBackupFailed",
        ),
        ("> 259200", "> 172800", "PiPeerBackupNotReporting"),
        ("[72h]", "[48h]", "PiPeerBackupNotReporting"),
        (
            'deployment="cloudflare-ddns-proxied"',
            'deployment="cloudflare-ddns-proxy"',
            "CloudflareDdnsUnavailable",
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
    problems = literal_problems(rules_by_name(mutated))
    assert any(p.startswith(alert) for p in problems), problems
