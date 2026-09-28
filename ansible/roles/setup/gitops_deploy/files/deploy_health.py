# ansible/roles/setup/gitops_deploy/files/deploy_health.py
"""The Discord delivery queue's pure half.

`apply_send_result` / `cap_pending` / `apply_drain_result` keep the pending-alert file bounded
across ticks. The module's name is from when it also held the Docker health gate.
"""


def apply_send_result(
    pending: dict[str, str], key: str, content: str, delivered: bool
) -> dict[str, str]:
    """The pending-alert queue after attempting to send `content` under `key`.

    On a confirmed delivery the key is cleared; on a failure the content is (re)queued under it, so a
    transient webhook blip can't permanently drop a post-merge alert (the ff-merged secrets/tasks/meta
    paths never re-reach their alert code). Pure: the caller (`deliver` in gitops_deploy.py) does the
    discord() I/O and persists the result only when it differs from the input. Returns a NEW dict;
    `pending` is not mutated.
    """
    updated = dict(pending)
    if delivered:
        updated.pop(key, None)
    else:
        updated[key] = content
    return updated


# The pending-alert queue had no cap, no expiry and no timestamps. Nothing reads the file back
# except drain_pending(), so an entry only leaves on a confirmed 2xx — a webhook that stays broken
# (a revoked URL, a permanently wrong channel) grows it without bound on a 30-minute tick, and
# every tick then re-POSTs the whole backlog. 64 is the cap: alerts are Discord messages, so the
# file stays well under a megabyte, and a backlog deeper than 64 means the webhook has been broken
# for over a day — at which point the OLDEST alerts are the least worth keeping.
#
# Eviction is oldest-first with no timestamp needed: dicts preserve insertion order, and json.load
# preserves it on the way back in, so the queue's own order IS its age order.
PENDING_ALERTS_MAX = 64


def cap_pending(
    pending: dict[str, str], max_entries: int = PENDING_ALERTS_MAX
) -> tuple[dict[str, str], list[str]]:
    """The queue trimmed to `max_entries`, plus the keys dropped — oldest first.

    Returns the dropped keys rather than dropping them silently: an alert discarded without a
    trace is the failure this queue exists to prevent, one level up. Pure; the caller logs.
    """
    if max_entries <= 0 or len(pending) <= max_entries:
        return pending, []
    dropped = list(pending)[: len(pending) - max_entries]
    return {k: c for k, c in pending.items() if k not in set(dropped)}, dropped


def apply_drain_result(pending: dict[str, str], delivered: set[str]) -> dict[str, str]:
    """Return the queue after a drain pass that confirmed the `delivered` keys were sent.

    Every other entry is kept for the next tick. Pure; the caller (`drain_pending`) does the
    per-entry discord() I/O and persists only on a change.
    """
    return {k: c for k, c in pending.items() if k not in delivered}
