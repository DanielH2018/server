"""Declare healthchecks' Discord notification channel instead of leaving it as UI state.

WHY THIS EXISTS. A Healthchecks integration is a `hc.api.models.Channel` row in the sqlite
database on the `healthchecks-config` PVC — live state no manifest reproduces. The row this
script writes was created by hand on 2024-09-22 and delivered fine (last on 2026-08-23), so
the gap was never that alerts went nowhere: it was that nothing but that volume held the
channel. A Longhorn restore past its creation, or anyone recreating the PVC, drops it
silently, and healthchecks' own SMTP path cannot cover the loss — outbound mail has been
deliberately broken since `healthchecks_smtp_password` was retired (2026-08-30).

IT ADOPTS THE ROW, IT DOES NOT ADD ONE. The match key is (project, kind, name) — the same
`webhook`-kind channel named `Discord` that already exists. Writing a second channel at the
same webhook would double every alert, and deleting the first from a deploy path would mean
destroying live rows on a name match.

HOW IT RUNS. `tasks/main.yml` pipes this file, plus a trailing `main()`, into `manage.py
shell` inside the pod. Nothing here runs at import, so the repo's tests import the module
and exercise the decision without Django.

WHERE THE URL COMES FROM. The pod's environment, keyed `HOMELAB_DISCORD_WEBHOOK_URL`,
injected by `templates/secret.yaml.j2` from the SOPS `healthchecks_discord_webhook_url`.
Nothing here calls sops and nothing prints the URL, so no transcript can capture it.

BOTH HALVES OF THE SPEC OR NEITHER. `Channel.webhook_spec(status)` reads
`method_`/`url_`/`body_`/`headers_` for the status it is given, and `sendalerts` asks for
`down` on the failing flip and `up` on the recovery. A value carrying only the `down` keys
raises inside the alert loop the first time a check comes back. `desired_spec` writes all
eight, and `tests/test_seed_discord_channel.py` reads them back the way the transport does.
"""

from __future__ import annotations

import json
import os

# The env key is ours, not a Django setting — healthchecks reads DISCORD_CLIENT_ID/SECRET for
# its OAuth "add integration" flow and ignores this name whatever it holds. The `HOMELAB_`
# prefix keeps that true if upstream ever adds a DISCORD_WEBHOOK_URL setting of its own.
WEBHOOK_ENV = "HOMELAB_DISCORD_WEBHOOK_URL"

# (kind, name) is the match key. Both name the row that has been live since 2024-09-22 —
# changing either adopts nothing and creates a second channel at the same Discord webhook,
# which is two messages per flip rather than one.
CHANNEL_KIND = "webhook"
CHANNEL_NAME = "Discord"

# The payload, transcribed from the live row rather than invented: healthchecks substitutes
# `$NAME` with the check's name, and Discord reads `content` as the message text.
BODY_DOWN = '{"content": "$NAME is down"}'
BODY_UP = '{"content": "$NAME is back up"}'
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# The last line of stdout. tasks/main.yml turns the count into Ansible's changed flag, so a
# no-op deploy has to be able to say zero rather than print nothing.
CHANGED_PREFIX = "SEED_CHANGED:"


def desired_spec(url: str) -> dict[str, object]:
    """The `Channel.value` document a webhook channel carries for `url`."""
    return {
        "name": CHANNEL_NAME,
        "method_down": "POST",
        "url_down": url,
        "body_down": BODY_DOWN,
        "headers_down": dict(HEADERS),
        "method_up": "POST",
        "url_up": url,
        "body_up": BODY_UP,
        "headers_up": dict(HEADERS),
    }


def desired_value(url: str) -> str:
    """`desired_spec` as the JSON string healthchecks stores."""
    return json.dumps(desired_spec(url), sort_keys=True)


def stored_spec(value: str) -> dict[str, object] | None:
    """The document inside a stored `Channel.value`, or None when it holds no usable one.

    Tolerates every shape a hand-made or half-written row can be in — empty, not JSON, JSON
    that is not an object — because each means the same thing to the caller: what is stored
    is not what we declare, so rewrite it.
    """
    if not value:
        return None
    try:
        doc = json.loads(value)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def needs_update(value: str, url: str) -> bool:
    """True when the stored channel value is not already exactly what we declare.

    Compares parsed documents, not text: healthchecks' own form writes the same keys in its
    own order, and a rewrite on every deploy would report a change that is not one.
    """
    return stored_spec(value) != desired_spec(url)


def main() -> None:
    # Imported here, not at module scope, so pytest imports this file with no Django present.
    # Project from its OWN module: `hc.api.models` imports it and so re-exports it by accident,
    # which stops working the day someone tidies that import.
    from hc.accounts.models import Project
    from hc.api.models import Channel

    if WEBHOOK_ENV not in os.environ:
        # Absent, not empty. The Secret carries the key unconditionally, so an absent variable
        # means the exec landed on a pod predating the current Secret. Failing here is the
        # point: the alternative reports "nothing to configure" and leaves the row unwritten.
        raise SystemExit(
            f"{WEBHOOK_ENV} is not in this pod's environment — the exec hit a stale pod, "
            "or the healthchecks Secret was applied without it"
        )

    url = os.environ[WEBHOOK_ENV].strip()
    if not url:
        print("no webhook URL configured — leaving channels alone")
        print(f"{CHANGED_PREFIX} 0")
        return

    changes = 0
    for project in Project.objects.all():
        channel, created = Channel.objects.get_or_create(
            project=project,
            kind=CHANNEL_KIND,
            name=CHANNEL_NAME,
            defaults={"value": desired_value(url)},
        )
        if created:
            changes += 1
        elif needs_update(channel.value, url) or channel.disabled:
            # healthchecks sets `disabled` itself after a permanent delivery failure (Discord
            # answers 404 to a deleted webhook). Clearing it is part of declaring the channel:
            # a rotated secret has to bring the integration back without a click.
            channel.value = desired_value(url)
            channel.disabled = False
            channel.save()
            changes += 1

        # Additive, never `set()`: a check assigned to this channel by hand stays assigned, and
        # a check created since the last deploy is picked up here.
        assigned = set(channel.checks.values_list("id", flat=True))
        missing = [c for c in project.check_set.all() if c.id not in assigned]
        if missing:
            channel.checks.add(*missing)
            changes += len(missing)

        print(
            f"project={project.name!r} channel={channel.code} created={created} "
            f"disabled={channel.disabled} checks_assigned={len(assigned) + len(missing)} "
            f"newly_assigned={len(missing)} last_notify={channel.last_notify} "
            f"last_error={channel.last_error!r}"
        )

    print(f"{CHANGED_PREFIX} {changes}")
