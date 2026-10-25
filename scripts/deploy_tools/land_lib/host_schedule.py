"""The schedule half of the host wait: when a host should have applied a merge.

A target's deadline is the second updater fire after the merge plus 10 minutes, read from the
first parent's rendered schedule (H3.9a). `host_wait` re-exports these names.
"""

from datetime import UTC, datetime

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.systemd_timer import DAY_S, calendar_fires, span_s

DEADLINE_SLACK_S = 10 * 60
# Triggers that fire once after boot, so they add no fire to a host that is up.
BOOT_ONLY = frozenset({"OnBootSec", "OnStartupSec"})


def calendar_after(expressions: list[str], after: float, count: int) -> list[float]:
    """The first `count` fires after `after` of daily `OnCalendar=` expressions, read in UTC."""
    daily = sorted({t for e in expressions for t in calendar_fires(e)})
    if not daily:
        return []
    day = after - after % DAY_S
    fires: list[float] = []
    while len(fires) < count:
        fires.extend(day + t for t in daily if day + t > after)
        day += DAY_S
    return fires[:count]


def deadline(target, merged_at: float) -> float | None:
    """When `target` should have applied the merge: its second updater fire after, plus slack.

    With several triggers, each bounds the second fire on its own, so the nearest bound wins.

    Returns:
        The deadline as a Unix time, or None when the schedule is unknown or unreadable.
    """
    if not target.triggers:
        return None
    bounds = []
    try:
        for key, values in target.triggers.items():
            if key == "OnCalendar":
                bounds.append(calendar_after(values, merged_at, 2)[1])
            elif key == "OnUnitActiveSec":
                bounds.append(merged_at + 2 * min(span_s(v) for v in values))
            elif key not in BOOT_ONLY:
                return None
    except ValueError, IndexError:
        return None
    if not bounds:
        return None
    return min(bounds) + target.delay + DEADLINE_SLACK_S


def next_fire(target, now: float) -> float | None:
    """The target's next calendar fire after `now`, or None when it has none this can name."""
    try:
        fires = calendar_after((target.triggers or {}).get("OnCalendar", []), now, 1)
    except ValueError:
        return None
    return fires[0] if fires else None


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
