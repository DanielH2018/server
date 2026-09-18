"""The Discord payload template every Kuma monitor shares renders to a payload Discord accepts.

Kuma renders `files/discord-message.liquid` with liquidjs 10.26 and POSTs the result verbatim,
so a template that renders to broken JSON, or to an embed past Discord's limits, drops every
DOWN alert at once — Kuma logs `Cannot send notification` and does not retry. The UI's Test
button cannot prove the DOWN path: it renders with `heartbeatJSON` null, and Liquid renders a
missing key as empty text. So the three real shapes are rendered here — a DOWN with tags, an
UP with `lastDownTime`, and the null Test context — and each result is parsed.

The engine here is python-liquid, not liquidjs. The template stays in the subset both accept:
`assign x = a == b` is the one divergence found (liquidjs takes it, python-liquid refuses it),
which is why the template sets `down` through an `if`. The pinned liquidjs rendered the same
three contexts to the same payloads on 2026-09-18.
"""

import json

import liquid
from liquid.extra import JSON

from _helpers import ANSIBLE
from _kuma_entities import _entities

TEMPLATE = ANSIBLE / "roles/k8s/uptime-kuma/files/discord-message.liquid"

# Discord's embed limits (title, field value, whole embed) — a payload past any one is a 400.
TITLE_MAX, FIELD_VALUE_MAX, EMBED_MAX = 256, 1024, 6000

# The longest heartbeat message a producer can push: bridge.net.PUSH_MSG_MAX and the cap in
# kuma-push-lib.sh are both 900.
PUSH_MSG_MAX = 900

DOWN = {
    "monitorJSON": {
        "name": "k3s Workload Health",
        "type": "push",
        "url": "",
        "description": "monitor-bridge's rollout gate. A DOWN names the workload.",
        "tags": [
            {"name": "severity", "value": "critical"},
            {
                "name": "runbook",
                "value": "https://docs.example/runbooks/workload-health/",
            },
        ],
    },
    "heartbeatJSON": {
        "status": 0,
        "msg": 'qbittorrent: 0/1 ready ("back-off restarting")\n' + "x" * PUSH_MSG_MAX,
        "time": "2026-09-18 21:10:04.000",
        "localDateTime": "2026-09-18 16:10:04",
        "timezone": "America/Chicago",
    },
    "hostnameOrURL": "",
    "msg": "[k3s Workload Health] [🔴 Down] qbittorrent: 0/1 ready",
}
UP = {
    "monitorJSON": {
        "name": "k3s Grafana",
        "type": "http",
        "url": "https://g",
        "tags": [],
    },
    "heartbeatJSON": {
        "status": 1,
        "msg": "200 - OK",
        "time": "2026-09-18 21:12:00.000",
        "localDateTime": "2026-09-18 16:12:00",
        "timezone": "America/Chicago",
        "lastDownTime": "2026-09-18 20:00:00.000",
    },
    "hostnameOrURL": "https://g",
    "msg": "[k3s Grafana] [✅ Up] 200 - OK",
}
TEST_BUTTON = {
    "monitorJSON": None,
    "heartbeatJSON": None,
    "hostnameOrURL": "testing.hostname",
    "msg": "Testing",
}


def _render(context, source=None):
    env = liquid.Environment()
    env.filters["json"] = JSON()
    text = source if source is not None else TEMPLATE.read_text()
    return json.loads(env.from_string(text).render(**context))


def _embed_size(embed):
    fields = embed.get("fields", [])
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
        + sum(len(f["name"]) + len(f["value"]) for f in fields)
    )


def test_a_down_renders_a_red_embed_naming_the_failure_and_the_tags():
    payload = _render(DOWN)
    (embed,) = payload["embeds"]
    assert embed["title"] == "🔴 k3s Workload Health is DOWN"
    assert embed["color"] == 15548997
    assert embed["description"].startswith("monitor-bridge's rollout gate")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["What failed"].startswith(
        'qbittorrent: 0/1 ready ("back-off restarting")'
    )
    assert fields["Severity"] == "critical"
    assert fields["Runbook"].endswith("/workload-health/")
    assert "Target" not in fields, "a push monitor has no address to show"


def test_a_recovery_renders_green_and_names_when_it_went_down():
    payload = _render(UP)
    (embed,) = payload["embeds"]
    assert embed["title"] == "🟢 k3s Grafana recovered"
    assert embed["color"] == 5763719
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Target"] == "https://g"
    assert fields["Down since"] == "2026-09-18 20:00:00.000 UTC"
    assert "Severity" not in fields and "Runbook" not in fields


def test_the_test_button_context_still_renders_a_payload():
    assert _render(TEST_BUTTON) == {"username": "Uptime Kuma", "content": "Testing"}


def test_the_longest_push_message_stays_inside_discords_limits():
    (embed,) = _render(DOWN)["embeds"]
    assert len(embed["title"]) <= TITLE_MAX
    for field in embed["fields"]:
        assert len(field["value"]) <= FIELD_VALUE_MAX, field["name"]
    assert _embed_size(embed) <= EMBED_MAX


def test_a_message_with_quotes_and_newlines_cannot_break_the_json():
    hostile = json.loads(json.dumps(DOWN))
    hostile["heartbeatJSON"]["msg"] = (
        'a "quoted" line\nsecond line \\ backslash {"k": 1}'
    )
    fields = {f["name"]: f["value"] for f in _render(hostile)["embeds"][0]["fields"]}
    assert fields["What failed"] == hostile["heartbeatJSON"]["msg"]


def test_the_notification_ships_this_template_as_a_webhook_body():
    config = _entities()["discord.json"]["config"]
    assert config["type"] == "webhook"
    assert config["webhookContentType"] == "custom"
    # lookup('file') strips the trailing newline; the render is otherwise byte-identical.
    assert config["webhookCustomBody"] == TEMPLATE.read_text().rstrip("\n")
    assert json.loads(config["webhookAdditionalHeaders"]) == {
        "Content-Type": "application/json"
    }, "axios posts a string body as form-urlencoded unless the header says otherwise"


def test_a_template_that_renders_broken_json_is_refused():
    broken = TEMPLATE.read_text().replace('"color":', '"color"')
    try:
        _render(DOWN, source=broken)
    except json.JSONDecodeError:
        return
    raise AssertionError("a payload that is not JSON rendered without complaint")
