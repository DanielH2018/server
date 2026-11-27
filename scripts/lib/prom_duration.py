"""Prometheus durations such as `90s` or `1h30m`, in seconds. Stdlib only: probe_lib reaches it."""

import re

UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
# Prometheus takes each unit at most once, largest first, in ASCII digits.
_DURATION = re.compile(
    "".join(rf"(?:(?P<{unit}>\d+){unit})?" for unit in reversed(UNIT_SECONDS)),
    re.ASCII,
)


def seconds(text: str, units: frozenset[str] = frozenset(UNIT_SECONDS)) -> float | None:
    """`text` in seconds, or None unless Prometheus accepts it using only the units in `units`."""
    match = _DURATION.fullmatch(text)
    if not match:
        return None
    parts = {u: int(n) for u, n in match.groupdict().items() if n is not None}
    if not parts or parts.keys() - units:
        return None
    return sum(n * UNIT_SECONDS[u] for u, n in parts.items())
