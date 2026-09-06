"""Declare healthchecks' Discord notification channel instead of clicking it into the UI.

WHY THIS EXISTS. A Healthchecks integration is a `hc.api.models.Channel` row in the sqlite
database on the `healthchecks-config` PVC — live state no manifest reproduces. The role
templated SMTP settings and nothing else, and outbound mail has been deliberately broken
since `healthchecks_smtp_password` was retired (2026-08-30), so a check that went down
flipped red in the UI and reached nobody. A Longhorn restore, or anyone recreating the
volume, drops a hand-made channel just as silently.

HOW IT RUNS. `tasks/main.yml` pipes this file, plus a trailing `main()`, into `manage.py
shell` inside the pod. Nothing here runs at import, so the repo's tests import the module
and exercise the decision without Django.

WHERE THE URL COMES FROM. The pod's environment, keyed `HOMELAB_DISCORD_WEBHOOK_URL`,
injected by `templates/secret.yaml.j2` from the SOPS `healthchecks_discord_webhook_url`.
Nothing here calls sops and nothing prints the URL, so no transcript can capture it.

THE VALUE SHAPE IS THE TRANSPORT'S. healthchecks' Discord transport reads
`self.json["webhook"]["url"]` (`hc/api/models.py`, `Channel.discord_webhook_url`) and POSTs
a Slack-shaped payload to that URL plus `/slack`. `desired_value` writes exactly that
shape, and `tests/test_seed_discord_channel.py` reads it back through the same key path.
"""

from __future__ import annotations

import json
import os

# The env key is ours, not a Django setting — healthchecks reads DISCORD_CLIENT_ID/SECRET for
# its OAuth "add integration" flow and ignores this name whatever it holds. The `HOMELAB_`
# prefix keeps that true if upstream ever adds a DISCORD_WEBHOOK_URL setting of its own.
WEBHOOK_ENV = "HOMELAB_DISCORD_WEBHOOK_URL"

CHANNEL_KIND = "discord"
# The row is matched on (project, kind, name), so renaming this orphans the old channel and
# creates a second one. It is also the name the Healthchecks Integrations page shows.
CHANNEL_NAME = "homelab-discord"

# The last line of stdout. tasks/main.yml turns the count into Ansible's changed flag, so a
# no-op deploy has to be able to say zero rather than print nothing.
CHANGED_PREFIX = "SEED_CHANGED:"


def desired_value(url: str) -> str:
    """The `Channel.value` JSON a Discord channel carries for `url`."""
    return json.dumps({"webhook": {"url": url}})


def stored_url(value: str) -> str | None:
    """The webhook URL inside a stored `Channel.value`, or None when it holds no usable one.

    Tolerates every shape a hand-made or half-written row can be in — empty, not JSON, JSON
    of the wrong shape — because each means the same thing to the caller: what is stored is
    not the URL we want, so rewrite it.
    """
    if not value:
        return None
    try:
        doc = json.loads(value)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    webhook = doc.get("webhook")
    if not isinstance(webhook, dict):
        return None
    url = webhook.get("url")
    return url if isinstance(url, str) and url else None


def needs_update(value: str, url: str) -> bool:
    """True when the stored channel value does not already carry exactly `url`."""
    return stored_url(value) != url


def main() -> None:
    # Imported here, not at module scope, so pytest imports this file with no Django present.
    # Project from its OWN module: `hc.api.models` imports it and so re-exports it by accident,
    # which stops working the day someone tidies that import.
    from hc.accounts.models import Project
    from hc.api.models import Channel

    if WEBHOOK_ENV not in os.environ:
        # Absent, not empty. The Secret carries the key unconditionally, so an absent variable
        # means the exec landed on a pod predating the current Secret. Failing here is the
        # point: the alternative reports "nothing to configure" and leaves the channel unwritten.
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
