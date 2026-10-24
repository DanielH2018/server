"""Cron and systemd `OnCalendar` expansion for the k3s restart window test.

Both parsers expand a schedule into the seconds of one day it fires, for a given date. They
accept the forms this tree uses and raise `Unparseable` on any other, so a new form fails the
test instead of passing as "never fires".
"""

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


class Unparseable(ValueError):
    """A schedule form this module does not expand."""


def _field(spec: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, raw = part.split("/", 1)
            step = int(raw)
        if part == "*":
            first, last = low, high
        elif ".." in part or "-" in part:
            first, last = (int(x) for x in re.split(r"\.\.|-", part))
        else:
            first = int(part)
            last = high if step != 1 else first
        values.update(range(first, last + 1, step))
    return values


def cron_fires(expr: str, day: date) -> list[int]:
    """Seconds of `day` at which a five-field cron expression fires."""
    fields = expr.split()
    if len(fields) != 5:
        raise Unparseable(f"cron {expr!r}")
    minute, hour, dom, month, dow = fields
    if day.month not in _field(month, 1, 12):
        return []
    weekday = {w % 7 for w in _field(dow, 0, 7)}
    dom_ok = day.day in _field(dom, 1, 31)
    dow_ok = (day.weekday() + 1) % 7 in weekday
    # Standard cron: with both fields restricted, either one matches.
    if dom != "*" and dow != "*":
        if not (dom_ok or dow_ok):
            return []
    elif not (dom_ok and dow_ok):
        return []
    return sorted(
        h * 3600 + m * 60 for h in _field(hour, 0, 23) for m in _field(minute, 0, 59)
    )


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_KEYWORDS = {
    "weekly": "Mon *-*-* 00:00:00",
    "daily": "*-*-* 00:00:00",
    "hourly": "*-*-* *:00:00",
}


def _weekdays(spec: str) -> set[int]:
    days: set[int] = set()
    for part in spec.split(","):
        first, _, last = part.partition("..")
        days.update(range(_DAYS.index(first[:3]), _DAYS.index((last or first)[:3]) + 1))
    return days


def calendar_fires(expr: str, day: date) -> list[int]:
    """Seconds of `day` (UTC) at which an `OnCalendar=` expression fires."""
    tokens = _KEYWORDS.get(expr.strip(), expr).split()
    zone = ZoneInfo(tokens.pop()) if tokens and "/" in tokens[-1] else None
    weekdays = None
    if tokens and re.match(r"[A-Za-z]", tokens[0]):
        weekdays = _weekdays(tokens.pop(0))
    date_spec = "*-*-*"
    if tokens and "-" in tokens[0]:
        date_spec = tokens.pop(0)
    clock = tokens.pop(0) if tokens else "00:00:00"
    if tokens or not re.fullmatch(r"[^:]+:[^:]+:[^:]+", clock):
        raise Unparseable(f"OnCalendar {expr!r}")
    if weekdays is not None and day.weekday() not in weekdays:
        return []
    _, month, dom = date_spec.split("-")
    if day.month not in _field(month, 1, 12) or day.day not in _field(dom, 1, 31):
        return []
    hour, minute, second = clock.split(":")
    fires = [
        h * 3600 + m * 60 + s
        for h in _field(hour, 0, 23)
        for m in _field(minute, 0, 59)
        for s in _field(second, 0, 59)
    ]
    if zone is None:
        return sorted(fires)
    shifted = []
    for seconds in fires:
        local = datetime.combine(day, time(), zone).replace(
            hour=seconds // 3600, minute=seconds % 3600 // 60, second=seconds % 60
        )
        utc = local.astimezone(ZoneInfo("UTC"))
        if utc.date() == day:
            shifted.append(utc.hour * 3600 + utc.minute * 60 + utc.second)
    return sorted(shifted)


def clock_text(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
