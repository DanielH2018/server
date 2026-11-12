"""The Alloy config must redact three credentials that pods print into their own logs.

- Flux's notification-controller logs the full Discord webhook URL, token included, when a post
  fails. The token lets its holder post to the alert channel.
- Traefik's access lines carry Home Assistant's `/api/webhook/<id>` path. A mobile_app
  registration's id authenticates the companion app.
- PeaNUT prints its session-signing `AUTH_SECRET` at every start, whether it generated the
  secret or read it from the environment.

Loki is reachable from the LAN with no Authelia in front of it, so each value would sit in a 744h
store anyone on the LAN can query. Each test checks both halves: the value is gone, and the rest
of the line that diagnoses the failure is kept.
"""

import pytest

from _alloy_pods_pipeline import ship

# Real shapes and lengths, with no entropy for gitleaks to read as credentials.
DISCORD_TOKEN = "x" * 68
HA_WEBHOOK_ID = "f" * 64
AUTH_SECRET = "A" * 43 + "="

DISCORD_LINE = (
    '{"level":"error","msg":"failed to send notification","error":"postMessage failed: failed to '
    "execute request: POST https://discord.com/api/webhooks/1556410786111426693/"
    f'{DISCORD_TOKEN}/slack giving up after 1 attempt(s): context deadline exceeded"}}'
)


@pytest.mark.parametrize(
    ("container", "namespace"),
    [("manager", "flux-system"), ("alertmanager", "observability")],
)
def test_the_discord_webhook_token_is_redacted_in_any_container(
    container: str, namespace: str
) -> None:
    out = ship(container, DISCORD_LINE, namespace=namespace)
    assert DISCORD_TOKEN not in out
    assert "api/webhooks/1556410786111426693/<redacted>/slack" in out
    assert "context deadline exceeded" in out


def test_the_ha_webhook_id_is_redacted_from_traefik_access_lines() -> None:
    line = (
        '{"ClientHost":"10.0.0.5","DownstreamStatus":500,"RequestHost":'
        f'"home-assistant.example","RequestMethod":"POST","RequestPath":"/api/webhook/{HA_WEBHOOK_ID}"'
        ',"RouterName":"home-assistant@kubernetescrd"}'
    )
    out = ship("access-log-rotate", line)
    assert HA_WEBHOOK_ID not in out
    assert '"RequestPath":"/api/webhook/<redacted>"' in out
    assert '"DownstreamStatus":500' in out


@pytest.mark.parametrize(
    "prefix", ["Generated new AUTH_SECRET: ", "AUTH_SECRET already exists: "]
)
def test_peanut_auth_secret_is_redacted_whether_generated_or_set(prefix: str) -> None:
    out = ship("peanut", prefix + AUTH_SECRET)
    assert AUTH_SECRET not in out
    assert out == prefix + "<redacted>"
