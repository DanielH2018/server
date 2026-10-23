"""The edge rules' thresholds equal the monitor-bridge values they port.

`files/rules/edge.yaml` is plain YAML, so each threshold is a literal that mirrors an env value
of the bridge row it shadows (M1 convention 1). A bridge value retuned without the rule would
leave the shadow soak comparing two different checks.

Run: uv run pytest ansible/tests/services/test_edge_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import render_pod_env
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "edge.yaml"
BRIDGE = K8S_ROLES / "monitor-bridge"
CONFIG_DEFAULTS = BRIDGE / "files" / "bridge" / "config_service.py"
SCRAPE_WINDOW_S = 300  # the [5m] every rate in the rules and in the bridge checks uses
CYCLE_MINUTES = 5  # the bridge's INTERVAL, which TRAEFIK_421_CONSECUTIVE counts


def bridge_env() -> dict[str, str]:
    """The pod env a deploy renders, which is where the bridge reads its thresholds."""
    return {k: str(v) for k, v in render_pod_env("monitor-bridge").items()}


def bridge_default(name: str) -> str:
    """The default `bridge/config_service.py` gives `name` when the env does not set it."""
    [value] = re.findall(rf'"{name}",\s*"([^"]*)"', CONFIG_DEFAULTS.read_text())
    return value


def rules_by_name(text: str) -> dict[str, dict]:
    """The edge group's rules, keyed by alert name."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def literal_problems(rules: dict[str, dict], env: dict[str, str], slow: dict[str, str]):
    """Each rule fragment the bridge value says should be present and is not."""
    stream = "|".join(
        p.removeprefix("homelab-").removesuffix("-")
        for p in env["TRAEFIK_STREAM_SERVICES"].split(",")
    )
    expected = {
        "CertExpiringSoon": [f"< {int(float(env['CERT_MIN_DAYS']))}"],
        "TraefikBackend5xx": [
            f"> {float(env['TRAEFIK_5XX_PCT']) / 100:g}",
            f">= {env['TRAEFIK_MIN_RPS']}",
        ],
        "TraefikBackendSlow": [
            f'le="{slow["TRAEFIK_SLOW_BUCKET"]}"',
            f"> {float(slow['TRAEFIK_SLOW_PCT']) / 100:g}",
            f"* {SCRAPE_WINDOW_S} >= {env['TRAEFIK_SLOW_MIN_REQUESTS']}",
            f">= {env['TRAEFIK_MIN_RPS']}",
            f'service!~"homelab-({stream})-.*"',
        ],
        "TraefikEntrypoint404Flood": [
            f"> {float(env['TRAEFIK_404_PCT']) / 100:g}",
            f">= {env['TRAEFIK_MIN_RPS']}",
        ],
        "TraefikRouter421": [f"> {env['TRAEFIK_421_RPS']}"],
    }
    problems = [
        f"{alert}: expr lacks `{fragment}`"
        for alert, fragments in expected.items()
        for fragment in fragments
        if fragment not in " ".join(rules[alert]["expr"].split())
    ]
    held = (int(env["TRAEFIK_421_CONSECUTIVE"]) - 1) * CYCLE_MINUTES
    if rules["TraefikRouter421"]["for"] != f"{held}m":
        problems.append(f"TraefikRouter421: for is not {held}m")
    return problems


def shipped() -> list[str]:
    slow = {
        name: bridge_default(name)
        for name in ("TRAEFIK_SLOW_BUCKET", "TRAEFIK_SLOW_PCT")
    }
    return literal_problems(rules_by_name(RULES.read_text()), bridge_env(), slow)


def test_edge_rule_literals_equal_the_bridge_values():
    assert shipped() == []


def test_a_retuned_bridge_value_is_flagged():
    text = RULES.read_text().replace("> 0.9", "> 0.5")
    slow = {"TRAEFIK_SLOW_BUCKET": "5.0", "TRAEFIK_SLOW_PCT": "5"}
    assert literal_problems(rules_by_name(text), bridge_env(), slow) == [
        "TraefikEntrypoint404Flood: expr lacks `> 0.9`"
    ]
