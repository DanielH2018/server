"""The secret rotation registry as typed rows, parsed once where the file is read.

`rotation_tools.load_registry` hands every caller a `Registry`, and `save_registry` writes one
back through `dump_registry`. A malformed file therefore fails at load with a `RegistryError`
naming the row, rather than deep inside `audit`.
"""

import datetime as dt
from dataclasses import dataclass, field

# The tier a row without one reads as.
DEFAULT_TIER = "assisted"
_ROW_FIELDS = frozenset({"tier", "last_rotated", "source"})


class RegistryError(ValueError):
    """A registry whose shape or values `parse_registry` refuses."""


@dataclass
class Entry:
    """One registered secret.

    Attributes:
      tier: the rotation tier. It is not checked against the tier table here, because
        `secret_registry.unknown_tiers` reports a tier with no cadence.
      last_rotated: the day the value last changed, or None for a tier with no cadence.
      source: `record` for a key SOPS only records, otherwise None.
    """

    tier: str = DEFAULT_TIER
    last_rotated: dt.date | None = None
    source: str | None = None


@dataclass
class Registry:
    """Every registered secret, keyed by its name in secrets.yml.

    Attributes:
      entries: one `Entry` per secret name.
    """

    entries: dict[str, Entry] = field(default_factory=dict)


def _date(name: str, value: object) -> dt.date | None:
    if value is None or type(value) is dt.date:
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise RegistryError(f"{name}: last_rotated {value!r} is not a YYYY-MM-DD date")


def _entry(name: str, row: object) -> Entry:
    if not isinstance(row, dict):
        raise RegistryError(f"{name}: a row is a mapping, got {type(row).__name__}")
    unknown = set(row) - _ROW_FIELDS
    if unknown:
        raise RegistryError(f"{name}: unknown field(s) {', '.join(sorted(unknown))}")
    tier = row.get("tier", DEFAULT_TIER)
    if not isinstance(tier, str):
        raise RegistryError(f"{name}: tier {tier!r} is not a string")
    source = row.get("source")
    if source is not None and not isinstance(source, str):
        raise RegistryError(f"{name}: source {source!r} is not a string")
    return Entry(tier, _date(name, row.get("last_rotated")), source)


def parse_registry(raw: object) -> Registry:
    """The registry a YAML load returned, as a `Registry`; None reads as an empty one.

    Raises:
      RegistryError: the document is not `{entries: {name: row}}`, a row carries a field
        other than `tier`, `last_rotated` and `source`, or a value has the wrong type.
    """
    if raw is None:
        return Registry()
    if not isinstance(raw, dict) or set(raw) - {"entries"}:
        raise RegistryError("the registry holds one top-level key, `entries`")
    rows = raw.get("entries") or {}
    if not isinstance(rows, dict):
        raise RegistryError("`entries` is a mapping of secret name to row")
    return Registry({name: _entry(name, row) for name, row in rows.items()})


def dump_registry(reg: Registry) -> dict:
    """`reg` as the plain mapping the registry file holds, dates as ISO strings."""
    rows = {}
    for name, entry in reg.entries.items():
        row: dict = {
            "tier": entry.tier,
            "last_rotated": entry.last_rotated.isoformat()
            if entry.last_rotated
            else None,
        }
        if entry.source is not None:
            row["source"] = entry.source
        rows[name] = row
    return {"entries": rows}
