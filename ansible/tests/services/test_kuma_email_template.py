"""The plain-text email Kuma sends on the break-glass tier renders for every context shape.

Same engine caveat as test_kuma_discord_template.py: python-liquid here, liquidjs in Kuma, so the
template stays in the shared subset. The email carries no JSON to break, so what these prove is
that each field lands where the reader expects it and that the null Test/cert-expiry context
still produces a body.
"""

import liquid

from _helpers import ANSIBLE
from _kuma_entities import _entities
from test_kuma_discord_template import DOWN, TEST_BUTTON, UP

TEMPLATE = ANSIBLE / "roles/k8s/uptime-kuma/files/email-message.liquid"


def _render(context, source=None):
    text = source if source is not None else TEMPLATE.read_text()
    ctx = dict(context)
    # Kuma builds `status` and `name` from the heartbeat and monitor (renderTemplate).
    if ctx.get("heartbeatJSON") is not None:
        ctx["status"] = "🔴 Down" if ctx["heartbeatJSON"]["status"] == 0 else "✅ Up"
        ctx["name"] = ctx["monitorJSON"]["name"]
    else:
        ctx["status"], ctx["name"] = "⚠️ Test", "Monitor Name not available"
    return liquid.Environment().from_string(text).render(**ctx)


def test_a_down_email_leads_with_the_status_and_the_failure():
    body = _render(DOWN)
    lines = body.splitlines()
    assert lines[0] == "🔴 Down: k3s Workload Health"
    assert lines[2].startswith(
        'What failed: qbittorrent: 0/1 ready ("back-off restarting")'
    )
    assert "Severity: critical" in body
    assert "Runbook: https://docs.example/runbooks/workload-health/" in body
    assert "monitor-bridge's rollout gate" in body
    assert "Target:" not in body


def test_a_recovery_email_names_when_it_went_down():
    body = _render(UP)
    assert body.splitlines()[0] == "✅ Up: k3s Grafana"
    assert "Status: 200 - OK" in body
    assert "Target: https://g" in body
    assert "Down since: 2026-09-18 20:00:00.000 UTC" in body
    assert "Severity" not in body and "Runbook" not in body


def test_the_null_context_prints_the_bare_message():
    assert _render(TEST_BUTTON) == "Testing"


def test_the_notification_ships_the_body_and_a_templated_subject_inside_tera_raw():
    config = _entities()["email.json"]["config"]
    assert config.get("htmlBody") is not True, "the break-glass tier stays plain text"
    body = config["customBody"]
    assert body.startswith("{% raw %}") and body.endswith("{% endraw %}")
    assert body.removeprefix("{% raw %}").removesuffix("{% endraw %}") == (
        TEMPLATE.read_text().rstrip("\n")
    )
    assert (
        config["customSubject"]
        == "{% raw %}[Homelab] {{ status }} {{ name }}{% endraw %}"
    )
    assert "{% endraw %}" not in TEMPLATE.read_text()
