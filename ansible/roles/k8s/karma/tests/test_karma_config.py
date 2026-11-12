"""Karma colours and ranks every `severity` value an alert rule carries.

A severity missing from `karma_severities` renders as a grey chip and sorts after every
ranked group, so a new critical-like value would read as the least urgent alert on screen.
"""

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from _k8s_render import render_role_template

RULE_DIRS = [
    K8S_ROLES / "observability" / "files" / "rules",
    K8S_ROLES / "loki-homelab" / "files" / "rules",
]


def _rule_severities() -> set[str]:
    found = set()
    for rule_dir in RULE_DIRS:
        for path in sorted(rule_dir.glob("*.yaml")):
            for group in (yaml_fast.safe_load(path.read_text()) or {}).get(
                "groups", []
            ):
                for rule in group.get("rules", []):
                    severity = rule.get("labels", {}).get("severity")
                    if severity:
                        found.add(severity)
    return found


def _karma_config() -> dict:
    (configmap,) = [
        d
        for d in yaml_fast.safe_load_all(
            render_role_template("karma", "configmap.yaml.j2")
        )
        if d
    ]
    return yaml_fast.safe_load(configmap["data"]["karma.yaml"])


def uncovered(severities: set[str], config: dict) -> list[str]:
    """Severities with no colour or no sort rank in a Karma config."""
    colours = {c["value"] for c in config["labels"]["color"]["custom"]["severity"]}
    ranks = set(config["grid"]["sorting"]["customValues"]["labels"]["severity"])
    return sorted(s for s in severities if s not in colours or s not in ranks)


def test_every_rule_severity_has_a_colour_and_a_rank():
    severities = _rule_severities()
    assert {"critical", "warning", "info"} <= severities, severities
    config = _karma_config()
    assert config["grid"]["sorting"]["label"] == "severity", config["grid"]
    assert uncovered(severities, config) == [], (
        "add each to karma_severities in ansible/roles/k8s/karma/defaults/main.yml"
    )


def test_a_severity_missing_from_the_config_is_flagged():
    config = _karma_config()
    config["labels"]["color"]["custom"]["severity"] = [
        c
        for c in config["labels"]["color"]["custom"]["severity"]
        if c["value"] != "info"
    ]
    assert uncovered({"critical", "info"}, config) == ["info"]
