"""The edge rules carry the thresholds monitor-bridge's five edge checks carried.

`files/rules/edge.yaml` is plain YAML, so each threshold is a literal. Until M6 batch A4's
Cutover-a2 each one mirrored an env value of the bridge row it shadowed, and this test read the
rendered env. That cutover deleted the rows and their env keys, so the numbers below are the
record of what the bridge carried (M1 convention 1), and a rule edit that drifts from them
fails here.

Run: uv run pytest ansible/tests/services/test_edge_rule_literals.py
"""

from _helpers import K8S_ROLES
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "edge.yaml"

# What the deleted env keys held (`templates/config/env.yaml.j2`) and the defaults the deleted
# `bridge/config_service.py` gave `TRAEFIK_SLOW_BUCKET` and `TRAEFIK_SLOW_PCT`.
CERT_MIN_DAYS = 14
TRAEFIK_5XX_PCT = 5
TRAEFIK_404_PCT = 90
TRAEFIK_421_RPS = "0.02"
TRAEFIK_421_HELD = "10m"  # TRAEFIK_421_CONSECUTIVE (3) cycles, 5 minutes apart: 2 x 5
TRAEFIK_MIN_RPS = "0.05"
TRAEFIK_SLOW_BUCKET = "5.0"
TRAEFIK_SLOW_PCT = 5
TRAEFIK_SLOW_MIN_REQUESTS = "3"
SCRAPE_WINDOW_S = 300  # the [5m] every rate in the rules and in the bridge checks uses

# Services whose traffic is dominated by LONG-LIVED CONNECTIONS, exempt from the latency ratio
# (the deleted `TRAEFIK_STREAM_SERVICES`). Matched as a prefix of the Traefik service label,
# `homelab-<name>-<hash>@kubernetescrd`, whose hash moves when an IngressRoute is renamed, so
# the rule matches `homelab-<name>-` and never the full label.
#
# Traefik's histogram times a request until the RESPONSE COMPLETES. For a websocket, an SSE
# stream or a Kubernetes watch that is the lifetime of the connection, so a healthy stream
# lands past every bucket edge by design. No bucket fixes this: the buckets are
# 0.1/0.3/1.2/5.0/+Inf and a stream outlives all of them. Measured on 2026-09-11, headlamp had
# 94.2% of requests under 0.1s and 3.1% past 5.0s, with a mean of 1.7s over the whole service,
# which puts the mean of that tail near 53s: Kubernetes watch streams. home-assistant and
# uptime-kuma are the other two offenders in the 30-day history (5 and 4 episodes), and are
# websocket UIs of the same shape.
#
# DELIBERATELY NOT HERE: bazarr and prowlarr, the other two names in that history. An *arr
# indexer search genuinely takes 5-30s and the user genuinely waits for it, so it is exactly
# the user-visible slowness the rule exists to catch. Excluding them would hide the signal
# rather than the artifact.
#
# DECIDED: jellyfin is not exempt either (#6649). Its slow requests in the 168h soak to
# 2026-10-30T18:32Z behind every DOWN episode were MKV font fetches
# (`/Videos/<id>/<id>/Attachments/<n>`, 10 KB-8 MB, 5-8s each) at playback start, 23-45 per
# episode: bounded responses the user waits on before subtitles render. Its `/socket`
# WebSockets are streams (median 1204s), but at most 3 finished in any 300s window and none
# moved the ratio past TRAEFIK_SLOW_PCT. Exempting the service would blind the rule to the
# attachment stall; a separate IngressRoute for `/socket` is the way to exempt the WebSockets
# alone.
STREAM_SERVICES = "headlamp|home-assistant|uptime-kuma"


def rules_by_name(text: str) -> dict[str, dict]:
    """The edge group's rules, keyed by alert name."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def literal_problems(rules: dict[str, dict]) -> list[str]:
    """Each rule fragment the recorded bridge value says should be present and is not."""
    expected = {
        "CertExpiringSoon": [f"< {CERT_MIN_DAYS}"],
        "TraefikBackend5xx": [f"> {TRAEFIK_5XX_PCT / 100:g}", f">= {TRAEFIK_MIN_RPS}"],
        "TraefikBackendSlow": [
            f'le="{TRAEFIK_SLOW_BUCKET}"',
            f"> {TRAEFIK_SLOW_PCT / 100:g}",
            f"* {SCRAPE_WINDOW_S} >= {TRAEFIK_SLOW_MIN_REQUESTS}",
            f">= {TRAEFIK_MIN_RPS}",
            f'service!~"homelab-({STREAM_SERVICES})-.*"',
        ],
        "TraefikEntrypoint404Flood": [
            f"> {TRAEFIK_404_PCT / 100:g}",
            f">= {TRAEFIK_MIN_RPS}",
        ],
        "TraefikRouter421": [f"> {TRAEFIK_421_RPS}"],
    }
    problems = [
        f"{alert}: expr lacks `{fragment}`"
        for alert, fragments in expected.items()
        for fragment in fragments
        if fragment not in " ".join(rules[alert]["expr"].split())
    ]
    if rules["TraefikRouter421"]["for"] != TRAEFIK_421_HELD:
        problems.append(f"TraefikRouter421: for is not {TRAEFIK_421_HELD}")
    return problems


def test_edge_rule_literals_equal_the_recorded_bridge_values():
    assert literal_problems(rules_by_name(RULES.read_text())) == []


def test_a_retuned_bridge_value_is_flagged():
    text = RULES.read_text().replace("> 0.9", "> 0.5")
    assert literal_problems(rules_by_name(text)) == [
        "TraefikEntrypoint404Flood: expr lacks `> 0.9`"
    ]
