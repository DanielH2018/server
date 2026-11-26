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
_COMPONENT = re.compile(r"(\d+)(ms|[smhdwy])")


def seconds(text: str, units: frozenset[str] = frozenset(UNIT_SECONDS)) -> float | None:
    """`text` in seconds, or None unless it is wholly components whose units are in `units`."""
    parts = _COMPONENT.findall(text)
    if (
        not parts
        or "".join(n + u for n, u in parts) != text
        or {u for _, u in parts} - units
    ):
        return None
    return sum(int(n) * UNIT_SECONDS[u] for n, u in parts)
