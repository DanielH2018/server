"""autoheal runs through the Pi's Compose project (H5.7).

Two guards moved here with the service from `ansible/tests/services/`, and both read the
rendered service of the real `hosts/daniel-pi/package.yml`, not the template text:

- The Discord webhook only notifies if its payload key is `content`. `willfarrell/autoheal`
  POSTs `{"<WEBHOOK_JSON_KEY>": "<message>"}` and defaults the key to `text`, which Discord
  answers with a 400. autoheal discards the response, so nothing else can see the failure. The
  guard asserts the implication between `WEBHOOK_URL` and `WEBHOOK_JSON_KEY`, exercised against
  an input that must pass and one that must be flagged.
- autoheal carries no healthcheck of its own. `AUTOHEAL_CONTAINER_LABEL=all` has no
  self-exclusion, so a probe makes autoheal its own patient and it restarts itself under the
  Pi's deploy load (#1789). `healthcheck: {disable: true}` is the accepted shape.

The webhook URL renders as `${monitor_discord_webhook_url}`, which the Compose env file fills,
so the guard keys on the field being set, never on its value.

Run: uv run pytest hosts/lib/tests/test_pi_compose_autoheal.py
"""

import pytest
from _compose_tree import COMPOSE, project_of

from renderer.renderer_lib import host as host_mode

# Discord's own field name. Named rather than derived: the image's default is the wrong one.
DISCORD_PAYLOAD_KEY = ("WEBHOOK_JSON_KEY", "content")
# The keys that hand Docker a probe. `interval:` counts: with `test:` omitted, Docker runs the
# image's own HEALTHCHECK at that cadence, which is the regression.
PROBE_KEYS = ("test", "interval", "timeout", "retries", "start_period")


@pytest.fixture(scope="module")
def autoheal():
    package = host_mode.render_host("daniel-pi")
    body = project_of(package)["files"][f"{COMPOSE}/autoheal/compose.yaml"]
    return body["services"]["autoheal"]


def _env(environment: list[str]) -> dict[str, str]:
    return dict(item.partition("=")[::2] for item in environment)


def notification_gaps(environment: list[str]) -> list[str]:
    """The notification properties a rendered compose environment lacks.

    Empty for a service that either notifies correctly or does not notify at all.
    """
    env = _env(environment)
    if not env.get("WEBHOOK_URL"):
        return []
    key, value = DISCORD_PAYLOAD_KEY
    return [] if env.get(key) == value else [f"{key}={value}"]


def self_watch_gaps(service: dict) -> list[str]:
    """Why a rendered compose service lets autoheal probe itself. Empty when it cannot."""
    healthcheck = service.get("healthcheck")
    if healthcheck is None:
        return ["no healthcheck: key, so the image's baked pgrep probe is inherited"]
    gaps: list[str] = []
    if healthcheck.get("disable") is not True:
        gaps.append("healthcheck: block lacks `disable: true`")
    gaps.extend(
        f"healthcheck carries `{key}:`, which hands Docker a probe to run"
        for key in PROBE_KEYS
        if key in healthcheck
    )
    return gaps


def test_the_shipped_service_notifies_discord_through_the_env_file_key(autoheal):
    env = _env(autoheal["environment"])
    assert env.get("WEBHOOK_URL") == "${monitor_discord_webhook_url}", (
        "autoheal's webhook must reach the container through the Compose env file"
    )
    assert not notification_gaps(autoheal["environment"]), (
        "autoheal posts to Discord without WEBHOOK_JSON_KEY=content, so every notification is "
        "rejected with a 400 and the restart goes unreported"
    )


def test_a_webhook_without_the_key_override_is_flagged():
    """REJECT: the image default and an unset key are the same 400."""
    url = "WEBHOOK_URL=https://discord.com/api/webhooks/1/x"
    assert notification_gaps([url]) == ["WEBHOOK_JSON_KEY=content"]
    assert notification_gaps([url, "WEBHOOK_JSON_KEY=text"]) == [
        "WEBHOOK_JSON_KEY=content"
    ]
    assert notification_gaps(["AUTOHEAL_CONTAINER_LABEL=all"]) == []


def test_the_shipped_service_gives_autoheal_no_probe(autoheal):
    assert self_watch_gaps(autoheal) == []


def test_an_interval_only_or_missing_healthcheck_is_flagged():
    """REJECT: `test` omitted inherits the image's probe, and so does no key at all."""
    gaps = self_watch_gaps({"healthcheck": {"interval": "60s"}})
    assert any("interval" in g for g in gaps), gaps
    assert any("disable" in g for g in gaps), gaps
    assert self_watch_gaps({"environment": []}) == [
        "no healthcheck: key, so the image's baked pgrep probe is inherited"
    ]


def test_autoheal_waits_for_the_sub_proxy_it_restarts_through(autoheal):
    """A list or a mapping keyed by service name; iterating either yields the names."""
    assert "docker-proxy-lifecycle" in list(autoheal["depends_on"])
    assert _env(autoheal["environment"])["DOCKER_SOCK"] == (
        "tcp://docker-proxy-lifecycle:2375"
    )
    assert autoheal["networks"] == ["lifecycle"]
