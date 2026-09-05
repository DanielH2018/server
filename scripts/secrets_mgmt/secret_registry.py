"""Pure logic over the rotation registry: seeding, reconciliation, due dates and drift.

Every function here takes the registry as a plain dict and returns a value or mutates that
dict — none of them read or write the file. `RotationTools.load_registry` /
`.save_registry` own the file, so the CLI can be driven end-to-end without touching disk.

Cadence arrives as a `tier_days` mapping, defaulting to `rotation_tools.DEFAULT_TIER_DAYS`.
`secret_rotation.py` spells the same table out as a literal because
`scripts/docs/gen_doc_fragments.py` AST-reads it from that file; nothing here may import the
entry point, so it reads the seam module's copy instead.
"""

from __future__ import annotations

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


def stagger_span(days: int) -> int:
    """Days of pull-earlier stagger `due_date` applies to a `days`-cadence tier."""
    return max(14, days // 12)


def seed_last_rotated(
    name: str, tier: str, today: dt.date, tier_days: Mapping = DEFAULT_TIER_DAYS
) -> str | None:
    """A staggered seed date.

    `due = seed + cadence - stagger` lands in [today+span+2, today+cadence], so nothing is
    overdue at registration and the due-dates are spread across the window. The seed span
    leaves room for `due_date`'s own stagger — both subtract, so the seed reserves 2*span.
    """
    days = tier_days[tier]
    if not days:
        return None
    span = stagger_span(days)
    offset = _stable_offset(name, days - 2 * span)
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


def due_date(
    name: str, entry: dict, tier_days: Mapping = DEFAULT_TIER_DAYS
) -> dt.date | None:
    """The date `name`'s secret comes due, or None when its tier has no cadence.

    The cadence carries a deterministic per-name stagger, and `name` is a required
    positional argument so that no caller can drop it and silently un-stagger the tier.
    Seeding staggers `last_rotated` once, at registration; that alone does not survive a
    rotation, because `rotate` stamps today's date on every secret in the batch and
    `advance_last_rotated` moves a hand-rotated one to its ciphertext's commit date. A
    batch event therefore used to collapse a whole tier onto one due-date and re-stamp the
    cluster intact every cycle. Staggering here instead makes the spread a property of the
    cadence, so it is re-derived after every rotation however `last_rotated` was set.

    The stagger only ever SUBTRACTS. `days` is the cadence published in
    `docs/secret-rotation.md` and the `secret-tiers` fragment, so a secret must never come
    due later than `last_rotated + days`; pulling it earlier stays inside that promise.
    """
    tier = entry.get("tier", "assisted")
    days = tier_days.get(tier)
    lr = entry.get("last_rotated")
    if not days or not lr:
        return None
    # A salted hash domain: the seed offset is drawn from the same name, and reusing it
    # unsalted would correlate the two subtractions instead of compounding the spread.
    offset = _stable_offset("due:" + name, stagger_span(days))
    return dt.date.fromisoformat(lr) + dt.timedelta(days=days - offset)


def audit(reg: dict, today: dt.date, tier_days: Mapping = DEFAULT_TIER_DAYS) -> dict:
    """Returns {overdue: [...], soon: [...], by_tier: {...}} sorted by urgency."""
    rows = []
    for name, entry in reg.get("entries", {}).items():
        d = due_date(name, entry, tier_days)
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
