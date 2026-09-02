#!/usr/bin/env python3
"""Generate docs/reference/secrets.md — the secret ROTATION REGISTRY, never any value.

WHAT THIS READS. ansible/secret_rotation.yml only. That file is plaintext on purpose and
holds names, tiers and dates — no values. It never reads ansible/vars/secrets.yml and never
invokes the decryption tool, because this page is committed and browsable behind SSO: a
generator able to read plaintext secrets is one bug away from publishing them.

scripts/docs/tests/test_gen_reference_secrets.py enforces that BEHAVIOURALLY: it records every path
build_rows() opens and asserts the registry is the only one. A source-text scan for "sops"
was the obvious alternative and is a bad test — the rendered page legitimately tells an
operator to run `sops`, so the scan fails on correct output while proving nothing about
what the code reads.

WHY IT IMPORTS secret_rotation. Due dates come from that module's TIER_DAYS and due_date(),
rather than a second implementation here. Two implementations of a due date drift, and the
page would then disagree with the audit cron that actually pages.

Usage::

    uv run python scripts/docs/gen_reference_secrets.py --out docs/reference/secrets.md
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import ANSIBLE
from secrets_mgmt import secret_rotation

REGISTRY = ANSIBLE / "secret_rotation.yml"

# What each tier means for an operator. The registry stores the tier name only.
TIER_NOTES = {
    "auto": "rotated unattended by the weekly secret-rotate cron",
    "assisted": "needs a human to mint the new value, then `secret_rotation.py rotate`",
    "external": "lives in a third-party system; rotate there first",
    "pinned": "DANGER — rotating it breaks decryption or locks out access. Follow the "
    "procedure in the runbook, never the generic rotate path",
    "ignore": "not rotated, and deliberately so",
}


def build_rows(
    registry: Path = REGISTRY, today: dt.date | None = None
) -> list[dict[str, str]]:
    """One row per registered secret: name, tier, last rotated, due, days left."""
    now = today or secret_rotation.today()
    reg = secret_rotation.load_registry(str(registry))
    rows = []
    for name, entry in sorted((reg.get("secrets") or {}).items()):
        tier = str(entry.get("tier", "unknown"))
        due = secret_rotation.due_date(entry)
        rows.append(
            {
                "name": name,
                "tier": tier,
                "last_rotated": str(entry.get("last_rotated", "unknown")),
                "due": due.isoformat() if due else "never (no interval for this tier)",
                "days_left": str((due - now).days) if due else "n/a",
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/gen_reference_secrets.py")]
    parts.append("# Secrets\n")
    parts.append(
        f"{len(rows)} secret(s) in the rotation registry "
        "(`ansible/secret_rotation.yml`).\n"
    )
    parts.append(
        '!!! note "Names and dates only"\n'
        "    This page is generated from the plaintext rotation registry. No secret VALUE "
        "is read here, and the generator never opens the encrypted store or invokes the "
        "decryption tool — a test enforces that.\n"
    )

    by_tier: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_tier.setdefault(row["tier"], []).append(row)

    # pinned first: it is the tier where following the generic procedure causes damage.
    order = ["pinned", "assisted", "external", "auto", "ignore"]
    tiers = [t for t in order if t in by_tier] + sorted(
        t for t in by_tier if t not in order
    )

    for tier in tiers:
        note = TIER_NOTES.get(tier, "unknown tier — not described in the generator")
        parts.append(f"\n## {tier}\n")
        parts.append(f"{note}.\n")
        parts.append("| Secret | Last rotated | Due | Days left |")
        parts.append("|---|---|---|---|")
        for row in by_tier[tier]:
            parts.append(
                f"| `{row['name']}` | {row['last_rotated']} | {row['due']} | "
                f"{row['days_left']} |"
            )

    parts.append(
        "\n## Rotating one\n\n"
        "`uv run python scripts/secrets_mgmt/secret_rotation.py audit` reports what is due. Adding a "
        "secret means `sops ansible/vars/secrets.yml`, then "
        "`secret_rotation.py sync`, then a commit — the `/add-secret` skill walks it. "
        "The `pinned` procedures are in [secret rotation](../secret-rotation.md) and are "
        "the ones to read before touching anything in that tier.\n"
    )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.registry)
    return finish_generator(
        "gen_reference_secrets", args.out, rows, render_markdown, "secret"
    )


if __name__ == "__main__":
    raise SystemExit(main())
