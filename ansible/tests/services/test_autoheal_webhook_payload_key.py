#!/usr/bin/env python3
"""autoheal's Discord webhook only notifies if its payload key is overridden.

`willfarrell/autoheal` POSTs `{"<WEBHOOK_JSON_KEY>": "<message>"}` and defaults that key to
`text` (read out of `/docker-entrypoint` on the live Pi container, 2026-09-10). Discord's
webhook API reads `content` and answers a `text` payload with a 400. So a compose file that
sets `WEBHOOK_URL` and leaves the key alone posts on every restart, gets rejected every time,
and notifies nobody — while the container stays healthy and the deploy reads green. Nothing
else in this repo can see that: the failure is in a response body autoheal discards.

The pairing is what makes the check real. A `WEBHOOK_URL`-only test would pass with the key
dropped, and a `WEBHOOK_JSON_KEY`-only test would pass with the URL dropped, so the property
asserted is the implication between them, exercised against an input that must be clean and an
input that must be flagged.

The check reads the RENDERED environment, not the template text (#2809). A substring scan
passed a template whose key line was commented out as `# - WEBHOOK_JSON_KEY=content`, which
ships the image's `text` default. The webhook URL renders as a stub, so the check keys on the
`WEBHOOK_URL` field being set, never on its value.

Run: uv run pytest ansible/tests/services/test_autoheal_webhook_payload_key.py
"""

from _compose_render import render_service

# Discord's own field name. Named rather than derived: the image's default is the wrong one,
# so there is nothing in the tree to derive it from.
DISCORD_PAYLOAD_KEY = ("WEBHOOK_JSON_KEY", "content")


def _env(environment: list[str]) -> dict[str, str]:
    return dict(item.partition("=")[::2] for item in environment)


def notification_gaps(environment: list[str]) -> list[str]:
    """The notification properties a rendered compose environment lacks.

    Empty for a service that either notifies correctly or does not notify at all — an
    autoheal with no `WEBHOOK_URL` is the pre-#1452 state, not a broken payload.
    """
    env = _env(environment)
    if not env.get("WEBHOOK_URL"):
        return []
    key, value = DISCORD_PAYLOAD_KEY
    return [] if env.get(key) == value else [f"{key}={value}"]


def test_the_shipped_template_notifies_discord() -> None:
    environment = render_service("autoheal")["environment"]
    assert _env(environment).get("WEBHOOK_URL"), (
        "autoheal sets no WEBHOOK_URL, so a restart on the Pi notifies nobody again"
    )
    assert not notification_gaps(environment), (
        "autoheal posts to Discord without WEBHOOK_JSON_KEY=content, so every notification is "
        "rejected with a 400 and the restart goes unreported"
    )


def test_a_webhook_without_the_key_override_is_flagged() -> None:
    """The rejecting half: the reader must fire on the exact regression it exists for."""
    assert notification_gaps(["WEBHOOK_URL=https://discord.com/api/webhooks/1/x"]) == [
        "WEBHOOK_JSON_KEY=content"
    ]


def test_the_image_default_key_is_flagged() -> None:
    """A key set to anything but Discord's field is the same 400 as leaving it unset."""
    assert notification_gaps(
        ["WEBHOOK_URL=https://discord.com/api/webhooks/1/x", "WEBHOOK_JSON_KEY=text"]
    ) == ["WEBHOOK_JSON_KEY=content"]


def test_no_webhook_at_all_is_not_flagged() -> None:
    assert notification_gaps(["AUTOHEAL_CONTAINER_LABEL=all"]) == []
