"""A systemd timer's triggers read from its unit text, and a daily `OnCalendar=` expanded.

Three readers share it: `test_renderer_host_update_limits.py`, which bounds the longest gap
between two updater fires, the landing's host wait (`land_lib/host_wait.py`), which needs the
fires after a merge, and `ansible/filter_plugins/updater_lag_grace.py`, which sizes
monitor-bridge's builder lag grace from the same gap (#6530). All read the timer as the host
renderer emits it, so none asks systemd on the machine it runs on.

Only the forms the host packages use are read. Anything else raises `ValueError`, so a
schedule neither reader can expand fails rather than reads as some other schedule.

Typical usage example:

    triggers, delay = timer_settings(unit_text, [dropin_text])
    fires = calendar_fires(triggers["OnCalendar"][0])
"""

import re

DAY_S = 86400

_SPAN = re.compile(r"(\d+)\s*(h|hr|min|m|s|sec)?")
_UNITS = {"h": 3600, "hr": 3600, "min": 60, "m": 60, "s": 1, "sec": 1, None: 1}
# Triggers that fire once after boot. They add no gap once the host is up.
_BOOT_ONLY = frozenset({"OnBootSec", "OnStartupSec"})
TRIGGERS = frozenset(
    {
        "OnActiveSec",
        "OnBootSec",
        "OnStartupSec",
        "OnUnitActiveSec",
        "OnUnitInactiveSec",
        "OnCalendar",
    }
)


def span_s(value: str) -> int:
    """A systemd time span such as `1h 20min` or `90`, in seconds.

    Raises:
        ValueError: The span uses a unit or form this module does not read.
    """
    parts = _SPAN.findall(value)
    if not parts or _SPAN.sub("", value).strip():
        raise ValueError(f"{value!r} is not a span this module reads")
    return sum(int(n) * _UNITS[unit or None] for n, unit in parts)


def _field(text: str, top: int) -> list[int]:
    """The values one OnCalendar time field matches: `*`, `a`, `a..b`, each with `/step`."""
    values = set()
    for part in text.split(","):
        body, _, step = part.partition("/")
        if body == "*":
            low, high = 0, top
        elif ".." in body:
            low, high = (int(n) for n in body.split(".."))
        else:
            low = int(body)
            high = top if step else low
        values.update(range(low, high + 1, int(step) if step else 1))
    return sorted(values)


def calendar_fires(expression: str) -> list[int]:
    """The seconds after midnight at which a daily `OnCalendar=` expression fires.

    Reads `*-*-* H:M[:S]` and `H:M[:S]` alone, each field a comma list of `*`, `a` or `a..b`,
    each with an optional `/step`.

    Raises:
        ValueError: The expression is not one this module reads.
    """
    words = expression.split()
    if len(words) == 2 and words[0] == "*-*-*":
        words = words[1:]
    fields = words[0].split(":") if len(words) == 1 else []
    if len(fields) == 2:
        fields.append("00")
    if len(fields) != 3 or not all(re.fullmatch(r"[\d*.,/]+", f) for f in fields):
        raise ValueError(
            f"OnCalendar={expression} is not a daily schedule this module reads"
        )
    hours, minutes, seconds = (
        _field(f, top) for f, top in zip(fields, (23, 59, 59), strict=True)
    )
    return sorted(
        h * 3600 + m * 60 + s for h in hours for m in minutes for s in seconds
    )


def longest_gap_s(unit: str, dropins: list[str]) -> int:
    """The longest a timer can go between two fires once the host is up, in seconds.

    Raises:
        ValueError: A trigger this module cannot bound, or no trigger that repeats.
    """
    triggers, delay = timer_settings(unit, dropins)
    bounds = []
    for key, values in triggers.items():
        if key == "OnCalendar":
            fires = sorted({t for v in values for t in calendar_fires(v)})
            bounds.append(
                max(
                    b - a
                    for a, b in zip(fires, [*fires[1:], fires[0] + DAY_S], strict=True)
                )
            )
        elif key == "OnUnitActiveSec":
            bounds.append(min(span_s(v) for v in values))
        elif key not in _BOOT_ONLY:
            raise ValueError(f"{key}= is not a trigger this module bounds")
    if not bounds:
        raise ValueError("the timer has no trigger that repeats")
    # Every trigger fires at least once within its own bound, so the nearest one wins.
    return min(bounds) + delay


def timeout_start_s(unit: str) -> int:
    """The unit's `TimeoutStartSec=` in seconds."""
    (value,) = re.findall(r"^TimeoutStartSec=(.+)$", unit, re.MULTILINE)
    return span_s(value)


def timer_settings(unit: str, dropins: list[str]) -> tuple[dict[str, list[str]], int]:
    """The triggers and `RandomizedDelaySec` of a timer with its drop-ins applied in order.

    An empty assignment to any trigger resets every trigger, as systemd reads it.

    Returns:
        Each trigger key mapped to its values in order, and the randomized delay in seconds.
    """
    triggers: dict[str, list[str]] = {}
    delay = 0
    for text in [unit, *dropins]:
        section = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
                continue
            key, _, value = (s.strip() for s in line.partition("="))
            if section != "[Timer]":
                continue
            if key in TRIGGERS:
                if value:
                    triggers.setdefault(key, []).append(value)
                else:
                    triggers = {}
            elif key == "RandomizedDelaySec":
                delay = span_s(value) if value else 0
    return triggers, delay
