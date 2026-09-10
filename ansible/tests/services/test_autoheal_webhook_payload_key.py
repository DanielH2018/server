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

Run: uv run pytest ansible/tests/services/test_autoheal_webhook_payload_key.py
"""

from _helpers import ANSIBLE

COMPOSE = (
    ANSIBLE
    / "roles"
    / "containers"
    / "autoheal"
    / "templates"
    / "docker-compose.yml.j2"
)

# Discord's own field name. Named rather than derived: the image's default is the wrong one,
# so there is nothing in the tree to derive it from.
DISCORD_PAYLOAD_KEY = "WEBHOOK_JSON_KEY=content"


def notification_gaps(text: str) -> list[str]:
    """The notification properties a compose template lacks.

    Empty for a template that either notifies correctly or does not notify at all — an
    autoheal with no `WEBHOOK_URL` is the pre-#1452 state, not a broken payload.
    """
    if "WEBHOOK_URL=" not in text:
        return []
    return [] if DISCORD_PAYLOAD_KEY in text else [DISCORD_PAYLOAD_KEY]


def test_the_shipped_template_notifies_discord() -> None:
    text = COMPOSE.read_text()
    assert "WEBHOOK_URL=" in text, (
        "autoheal sets no WEBHOOK_URL, so a restart on the Pi notifies nobody again"
    )
    assert not notification_gaps(text), (
        f"autoheal posts to Discord without {DISCORD_PAYLOAD_KEY}, so every notification is "
        "rejected with a 400 and the restart goes unreported"
    )


def test_a_webhook_without_the_key_override_is_flagged() -> None:
    """The rejecting half: the reader must fire on the exact regression it exists for."""
    assert notification_gaps("- WEBHOOK_URL=https://discord.com/api/webhooks/1/x") == [
        DISCORD_PAYLOAD_KEY
    ]


def test_no_webhook_at_all_is_not_flagged() -> None:
    assert notification_gaps("- AUTOHEAL_CONTAINER_LABEL=all") == []
