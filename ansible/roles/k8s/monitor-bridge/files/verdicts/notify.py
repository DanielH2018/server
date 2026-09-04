"""Notification-path verdicts for check.py — Discord webhook validity.

These decide; `checks/notify.py` fetches. `discord_webhook_ok` takes its inputs as arguments and
reads no module-level config, which is what makes it safe to live here — see bridge/parsing.py's
header for the rule and why breaking it fails silently rather than loudly.

Split out of verdicts/service.py on 2026-09-04, alongside verdicts/logs.py, for the same reason:
that module was the catch-all, and this verdict has exactly one consumer.
"""


def discord_webhook_ok(status_code: int, name: str | None = None) -> tuple[bool, str]:
    """Pure: does a GET on a Discord webhook return 200 (still valid)? (ok, msg).

    Discord answers a webhook GET with its JSON metadata (id/name) and HTTP 200 while the
    webhook exists, and 404 once it's been rotated/revoked/deleted — so a non-200 means the
    alert POSTs won't deliver. (A GET never posts a message, so this can't spam.)
    """
    if status_code == 200:
        return True, "Discord webhook valid%s" % (" (%s)" % name if name else "")
    return (
        False,
        "Discord webhook returned HTTP %s — alerts won't deliver" % status_code,
    )
