"""Declare the *arr Discord Connect notification instead of leaving it as UI state.

WHY THIS EXISTS. A Sonarr/Radarr "Connect" notification is a row in `sonarr.db` / `radarr.db`
on the app's config PVC — live state no manifest reproduces. Both rows this script writes
already existed when it was written (see the role's CLAUDE.md for the before-state), and both
deliver, so the gap was never that health alerts went nowhere. It was that nothing but that
volume held them: a Longhorn revert past their creation, or anyone recreating the claim, drops
the notification silently and the app goes quiet in exactly the failure it exists to report.

IT ADOPTS THE ROW, IT DOES NOT ADD ONE. The match key is (implementation=Discord, name), and
the default name is the one both live rows already carry. A second Discord notification at the
same webhook posts every event twice, so a name that adopts nothing is worse than no change at
all. This script never deletes: a notification it does not match is left alone.

WHAT IT DECLARES. The webhook URL, the posting username, `includeHealthWarnings`, and the FULL
trigger set — every `onX` key the app reports, set true when the caller names it and false
otherwise. Declaring only the true half would let a trigger enabled by hand survive a deploy
that is supposed to be the whole truth about when Discord is notified.

WHAT IT LEAVES ALONE. `grabFields` / `importFields` / `manualInteractionFields` and the
`avatar` / `author` overrides. The update path merges into the body the API returned rather
than building one from scratch, so those keep whatever they hold; the create path takes them
from `GET /api/v3/notification/schema`, which is where the app's own defaults live.

WHERE THE SECRETS COME FROM. The process environment — `ARR_API_KEY` and
`ARR_DISCORD_WEBHOOK_URL`, set by `tasks/main.yml` under `no_log`. Never argv, and nothing
here prints a field value: the output is the decision (created / updated / no-op) and the
`SEED_CHANGED: <n>` marker `changed_when` reads. `probe.py arr <app> notification` prints the
webhook URL in full, which is why verifying this by hand needs a filter and this script does
not need one.

RUN IT: `tasks/main.yml` invokes it with no arguments to seed, and with `--test` to make the
app POST a real Discord message through the resolved spec. Nothing runs at import, so the
tests exercise the decision without a cluster.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request

# Mirrors scripts/diagnostics/probe_lib/arr.py. The API is reached at the Service ClusterIP
# directly, NOT through Traefik: neither app has an Authelia bypass for /api/*, so a routed
# request 302s to the login page instead of reaching the app.
ARR_PORTS = {"sonarr": 8989, "radarr": 7878}
API_VERSION = "v3"

IMPLEMENTATION = "Discord"

# The last line of stdout. tasks/main.yml turns the count into Ansible's changed flag, so a
# no-op deploy has to be able to say zero rather than print nothing.
CHANGED_PREFIX = "SEED_CHANGED:"

HTTP_TIMEOUT = 30


class SeedError(Exception):
    """A condition the caller must fail on rather than report as configured."""


# ── the decision (pure, and the part the tests drive) ────────────────────────────────────────


def trigger_keys(body: dict) -> set[str]:
    """Every `onX` trigger key the app itself reports for this notification.

    Derived from the object rather than listed here: sonarr has `onImportComplete` and
    `onSeriesAdd`, radarr has `onMovieAdded`, and upstream adds more. A hardcoded list would
    silently stop covering a trigger the app grew, which is the exact hole this closes.
    `supportsOnX` is excluded — it is a capability flag, not a trigger.
    """
    return {
        key
        for key, value in body.items()
        if key.startswith("on") and isinstance(value, bool)
    }


def set_field(body: dict, name: str, value: object) -> None:
    """Set the named entry in the *arr `fields` list, in place.

    Raises when the field is absent: a silent skip would leave the webhook URL unwritten and
    still report the notification as declared.
    """
    for field in body.get("fields") or []:
        if field.get("name") == name:
            field["value"] = value
            return
    raise SeedError(f"the {IMPLEMENTATION} notification has no {name!r} field")


def declared_body(
    existing: dict,
    *,
    name: str,
    webhook_url: str,
    username: str,
    triggers: list[str],
    include_health_warnings: bool,
) -> dict:
    """`existing` with everything this role declares applied, as a new dict.

    A deep copy merged into, never a body built from scratch — the *arr API rejects a PUT
    missing `implementation`/`configContract`, and a body assembled here would blank the
    `grabFields`/`importFields` lists the app populated.
    """
    unknown = sorted(set(triggers) - trigger_keys(existing))
    if unknown:
        # A typo'd trigger is the failure mode with no symptom: the key is written, the app
        # ignores it, and the notification stays off while the deploy reports success.
        raise SeedError(
            f"trigger(s) {unknown} are not keys this notification carries — "
            f"known: {sorted(trigger_keys(existing))}"
        )

    body = copy.deepcopy(existing)
    body["name"] = name
    for key in trigger_keys(body):
        body[key] = key in triggers
    body["includeHealthWarnings"] = include_health_warnings
    set_field(body, "webHookUrl", webhook_url)
    set_field(body, "username", username)
    return body


def needs_update(existing: dict, desired: dict) -> bool:
    """True when the live notification is not already exactly what we declare.

    Compares parsed documents, not text — the API serialises its own key order, and a rewrite
    on every deploy would report a change that is not one.
    """
    return existing != desired


def schema_for(schemas: list[dict]) -> dict:
    """The Discord entry from `GET /api/v3/notification/schema`, without its `presets`.

    `presets` is UI scaffolding the API rejects on POST.
    """
    for entry in schemas:
        if entry.get("implementation") == IMPLEMENTATION:
            entry = copy.deepcopy(entry)
            entry.pop("presets", None)
            return entry
    raise SeedError(
        f"the app's notification schema has no {IMPLEMENTATION} implementation — "
        "it is not a version this role can seed"
    )


def find_notification(notifications: list[dict], name: str) -> dict | None:
    """The live Discord notification carrying `name`, or None.

    Matches on implementation AND name: a CustomScript called the same thing is a different
    integration, and overwriting it would silently retire someone's script hook.
    """
    for entry in notifications:
        if entry.get("implementation") == IMPLEMENTATION and entry.get("name") == name:
            return entry
    return None


# ── the transport ────────────────────────────────────────────────────────────────────────────


def request(base_url: str, api_key: str, path: str, method: str = "GET", body=None):
    """One *arr API call. The key travels as a header, never in the URL."""
    url = f"{base_url}/api/{API_VERSION}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        # The app answers a rejected spec with a JSON body naming the field. Surfacing it is
        # the difference between "the deploy failed" and knowing which key it refused. The URL
        # is in `url`, not in the response, so nothing secret comes back through here.
        raise SeedError(
            f"{method} {path} returned HTTP {exc.code}: {exc.read().decode()[:500]}"
        ) from exc
    except OSError as exc:
        raise SeedError(f"{method} {path} could not reach the app: {exc}") from exc
    return json.loads(raw) if raw.strip() else None


def env_or_fail(key: str) -> str:
    """A required environment value.

    Absent, not empty: `tasks/main.yml` sets every key unconditionally, so a missing one means
    the task was edited, and reporting "nothing to configure" would leave the row unwritten.
    """
    if key not in os.environ:
        raise SeedError(f"{key} is not in this process's environment")
    value = os.environ[key].strip()
    if not value:
        raise SeedError(f"{key} is empty")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--test",
        action="store_true",
        help="POST the resolved spec to /notification/test — sends a real Discord message",
    )
    args = parser.parse_args(argv)

    app = env_or_fail("ARR_APP")
    if app not in ARR_PORTS:
        raise SeedError(f"ARR_APP={app!r} is not one of {sorted(ARR_PORTS)}")
    base_url = f"http://{env_or_fail('ARR_HOST')}:{ARR_PORTS[app]}"
    api_key = env_or_fail("ARR_API_KEY")
    webhook_url = env_or_fail("ARR_DISCORD_WEBHOOK_URL")
    name = env_or_fail("ARR_NOTIFICATION_NAME")
    username = env_or_fail("ARR_NOTIFICATION_USERNAME")
    triggers = json.loads(env_or_fail("ARR_NOTIFICATION_TRIGGERS"))
    include_health_warnings = (
        env_or_fail("ARR_NOTIFICATION_INCLUDE_HEALTH_WARNINGS").lower() == "true"
    )

    def declare(base: dict) -> dict:
        return declared_body(
            base,
            name=name,
            webhook_url=webhook_url,
            username=username,
            triggers=triggers,
            include_health_warnings=include_health_warnings,
        )

    existing = find_notification(request(base_url, api_key, "notification"), name)
    if existing is None:
        desired = declare(schema_for(request(base_url, api_key, "notification/schema")))
        verb, path, action = "POST", "notification", "created"
    else:
        desired = declare(existing)
        verb, path, action = "PUT", f"notification/{existing['id']}", "updated"

    if args.test:
        # The app dials Discord itself with this spec and 2xx's only if the message landed.
        request(base_url, api_key, "notification/test", method="POST", body=desired)
        print(f"{app}: test message accepted by the app for {name!r}")
        print(f"{CHANGED_PREFIX} 0")
        return 0

    if existing is not None and not needs_update(existing, desired):
        print(f"{app}: {name!r} already declares this spec — no change")
        print(f"{CHANGED_PREFIX} 0")
        return 0

    request(base_url, api_key, path, method=verb, body=desired)
    on = sorted(key for key in trigger_keys(desired) if desired[key])
    print(f"{app}: {name!r} {action} — triggers {on}")
    print(f"{CHANGED_PREFIX} 1")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        # Message only, no traceback: the traceback would carry the environment on some
        # Python builds, and this script's environment holds two credentials.
        sys.exit(f"seed_arr_notification: {exc}")
