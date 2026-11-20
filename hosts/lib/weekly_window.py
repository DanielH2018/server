"""Whether a time falls inside a window around a weekly UTC instant.

The triage poller and the restart broker each ship a copy beside their script (a `lib:` entry
in their `component.yml`) and import it by bare name. Both ask whether the mute around the
weekly reboot is open. Stdlib only.

Typical usage example:

    from weekly_window import in_weekly_window

    muted = in_weekly_window(time.time(), 0, 7 * 60 + 30, before_min=5, after_min=45)
"""

from __future__ import annotations

import datetime as dt

WEEK_MIN = 7 * 1440


def in_weekly_window(
    now: float, weekday: int, minute_of_day: int, before_min: int, after_min: int
) -> bool:
    """True when `now` is within the window around the weekly instant.

    Args:
        now: epoch seconds.
        weekday: the instant's day in cron numbering, where 0 is Sunday.
        minute_of_day: the instant's minute of that day, in UTC.
        before_min: the window opens this many minutes before the instant.
        after_min: the window closes this many minutes after it.
    """
    t = dt.datetime.fromtimestamp(now, dt.UTC)
    week_min = ((t.weekday() + 1) % 7) * 1440 + t.hour * 60 + t.minute
    delta = (week_min - (weekday * 1440 + minute_of_day)) % WEEK_MIN
    return delta <= after_min or delta >= WEEK_MIN - before_min
