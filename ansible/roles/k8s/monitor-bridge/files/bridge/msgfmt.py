"""One grammar for a Kuma push message that names several things: group by reason, list names once.

Kuma renders a heartbeat's `msg` as raw plain text — no markdown, no wrapping, one table cell
(`src/pages/Details.vue`) — and hands the same string to every notification as the Discord
"What failed" field. So the text reads well exactly when the producer writes it well. The
shape this module refuses is the one Release Staleness pushed on 2026-09-17 (#2013): 57
services each carrying the SAME reason, the reason repeated 57 times, ~7,500 chars that
Discord rejected outright. The loop there ran per name; this one runs per reason.

The grammar, one line:

    <count> <unit>s <state> — <n> reasons. <reason> (<k>: a, b, c, +N). <reason> (…). Details: <cmd>

The headline stands alone in the first 60 chars, because Kuma's cell and the Discord field
both truncate from the right. Groups are ordered largest first. Over `limit`, names are
elided before anything else — the count is kept, the reasons are kept — and only then does
the hard cut apply. `probe.py releases --stale-only --kuma` renders through this module too
(a `sys.path` bootstrap onto this role's `files/`), so the cron and the pod agree on the shape.

Pure: no I/O, no bridge imports, so `probe.py` can load it without the pod's config.
"""

from collections.abc import Mapping

# Mirrors `bridge.net.PUSH_MSG_MAX`; not imported from there because net.py pulls in the pod's
# config at import time and this module must load under probe.py on a host.
LIMIT = 900
NAMES_PER_REASON = 5


def _plural(count: int, unit: str) -> str:
    return unit if count == 1 else unit + "s"


def _group(items: Mapping[str, str]) -> list[tuple[str, list[str]]]:
    """[(reason, [name, ...]), ...], largest group first, names sorted, ties by reason text."""
    by_reason: dict[str, list[str]] = {}
    for name, reason in items.items():
        by_reason.setdefault(reason, []).append(name)
    groups = [(reason, sorted(names)) for reason, names in by_reason.items()]
    groups.sort(key=lambda g: (-len(g[1]), g[0]))
    return groups


def _names(names: list[str], keep: int) -> str:
    shown = names[:keep]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f", +{more}" if more else "")


def _render(unit, state, groups, details, keep, total) -> str:
    if total == 1:
        (reason, (name,)) = groups[0]
        body = f"1 {unit} {state}: {name} — {reason}"
    elif len(groups) == 1:
        reason, names = groups[0]
        body = (
            f"{total} {_plural(total, unit)} {state} — {reason} ({_names(names, keep)})"
        )
    else:
        head = f"{total} {_plural(total, unit)} {state} — {len(groups)} reasons."
        parts = [
            f"{reason} ({len(names)}: {_names(names, keep)})"
            for reason, names in groups
        ]
        body = head + " " + ". ".join(parts)
    if details:
        body += f". Details: {details}"
    return body


def format_down(
    unit: str,
    state: str,
    items: Mapping[str, str],
    details: str | None = None,
    limit: int = LIMIT,
) -> str:
    """The grouped one-liner for `items` ({name: reason}); `unit` singular, e.g. "service".

    `state` is the predicate the count carries ("stale", "down", "over limit"). `details`
    names the command that prints the full list. An empty `items` is a caller bug and raises,
    because a DOWN message with nothing in it is the silence this module exists to prevent.
    """
    if not items:
        raise ValueError("format_down needs at least one item")
    groups = _group(items)
    total = len(items)
    for keep in range(NAMES_PER_REASON, 0, -1):
        msg = _render(unit, state, groups, details, keep, total)
        if len(msg) <= limit:
            return msg
    # Names are down to one per reason and it still does not fit: hard cut, marker last. The
    # marker's width depends on the count it carries, so settle it in two passes.
    dropped = len(msg)
    for _ in range(2):
        marker = f" …(+{dropped} chars)"
        keep_chars = limit - len(marker)
        dropped = len(msg) - keep_chars
    return msg[:keep_chars] + f" …(+{dropped} chars)"
