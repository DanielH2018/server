"""Pure logic over the rotation registry: seeding, reconciliation, due dates and drift.

Every function here takes the registry as a plain dict and returns a value or mutates that
dict — none of them read or write the file. `RotationTools.load_registry` /
`.save_registry` own the file, so the CLI can be driven end-to-end without touching disk.

Cadence arrives as a `tier_days` mapping, defaulting to `rotation_tools.DEFAULT_TIER_DAYS`.
`secret_rotation.py` spells the same table out as a literal because
`scripts/docs/gen_doc_fragments.py` AST-reads it from that file; nothing here may import the
entry point, so it reads the seam module's copy instead.
"""

import datetime as dt
import hashlib
from collections.abc import Mapping

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from secrets_mgmt.secret_classify import classify
from secrets_mgmt.rotation_tools import DEFAULT_TIER_DAYS


def _stable_offset(name: str, span: int) -> int:
    """Deterministic 0..span-1 from the name — spreads seed dates so due-dates fan out."""
    if span <= 0:
        return 0
    return int(hashlib.sha256(name.encode()).hexdigest(), 16) % span


def seed_last_rotated(
    name: str, tier: str, today: dt.date, tier_days: Mapping = DEFAULT_TIER_DAYS
) -> str | None:
    """A staggered seed date.

    `due = seed + cadence` lands in [today+lead, today+cadence], so nothing is overdue at
    registration and the due-dates are spread across the window.
    """
    days = tier_days[tier]
    if not days:
        return None
    lead = max(14, days // 12)
    offset = _stable_offset(name, days - lead)
    return (today - dt.timedelta(days=offset)).isoformat()


def sync(
    reg: dict, names: list[str], today: dt.date, tier_days: Mapping = DEFAULT_TIER_DAYS
) -> tuple[list[str], list[str]]:
    """Add missing secrets (classified + staggered seed); report stale registry entries."""
    entries = reg.setdefault("entries", {})
    added, stale = [], []
    for name in names:
        if name not in entries:
            tier = classify(name)
            entries[name] = {
                "tier": tier,
                "last_rotated": seed_last_rotated(name, tier, today, tier_days),
            }
            added.append(name)
    live = set(names)
    stale = sorted(n for n in entries if n not in live)
    return added, stale


def due_date(entry: dict, tier_days: Mapping = DEFAULT_TIER_DAYS) -> dt.date | None:
    """The date `entry`'s secret comes due, or None when its tier has no cadence."""
    tier = entry.get("tier", "assisted")
    days = tier_days.get(tier)
    lr = entry.get("last_rotated")
    if not days or not lr:
        return None
    return dt.date.fromisoformat(lr) + dt.timedelta(days=days)


def audit(reg: dict, today: dt.date, tier_days: Mapping = DEFAULT_TIER_DAYS) -> dict:
    """Returns {overdue: [...], soon: [...], by_tier: {...}} sorted by urgency."""
    rows = []
    for name, entry in reg.get("entries", {}).items():
        d = due_date(entry, tier_days)
        if d is None:
            continue
        rows.append((name, entry.get("tier"), d, (d - today).days))
    rows.sort(key=lambda r: r[3])
    overdue = [r for r in rows if r[3] < 0]
    soon = [r for r in rows if 0 <= r[3] <= 14]
    by_tier: dict[str, int] = {}
    for _, tier, _, days_left in rows:
        if days_left < 0:
            by_tier[tier] = by_tier.get(tier, 0) + 1
    return {"overdue": overdue, "soon": soon, "by_tier": by_tier, "all": rows}


def registry_drift(registered: set, present: set) -> tuple[list, list]:
    """Pure registry-vs-secrets.yml drift.

    Returns (missing, stale):
      missing = in secrets.yml but NOT in the registry (a `sync` was forgotten after /add-secret);
      stale   = a registry row whose secret was removed from secrets.yml.
    Reads plaintext key NAMES only — never decrypts a value, so it's CI-safe.
    """
    return sorted(present - registered), sorted(registered - present)
