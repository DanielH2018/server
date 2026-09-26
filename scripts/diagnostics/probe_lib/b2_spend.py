"""The `b2-spend` report: measured Class B backup spend, per volume and per backup target.

Split out of probe_lib/b2_ledger.py at its 600-line cap. Pure: every function here takes Loki
rows or an already-parsed mapping and returns a mapping or rendered text. Nothing runs a
command, and the only sibling it imports is the `B2_BACKUP_TARGET_NAME` constant.

b2_ledger.py keeps the ledger store and the `run_b2_spend` entry point that drives these.
"""

import re

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the import below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.longhorn_cluster import B2_BACKUP_TARGET_NAME


# Longhorn logs one of these per backup, naming the delta it processed:
#   "Created snapshot changed blocks: 46 mappings, 46 blocks and 45 new blocks"
# `blocks` is the delta it walks, and it issues one HeadObject per block to decide whether the
# block is already in the store — so that count IS the backup's Class B transaction cost. `new`
# is what it then uploaded, which is Class A and unmetered. B2 publishes no usage endpoint (its
# per-class counters are Partner-tier only), so this line is the closest thing to a meter the
# backup plane has, and it costs nothing because the logs are already in Loki.
BACKUP_BLOCKS_RE = re.compile(
    r"Created snapshot changed blocks: \d+ mappings, (\d+) blocks and (\d+) new blocks"
)
# The replica that emitted the line, e.g. "[pvc-1c0e18da-...-r-9d333575] time=..."
BACKUP_VOLUME_RE = re.compile(r"\[(pvc-[0-9a-f-]{36})-r-[0-9a-f]+\]")
SPEND_LOGQL = '{namespace="longhorn-system"} |= "Created snapshot changed blocks"'


def parse_backup_spend(rows):
    """[(ns_timestamp, line)] -> per-volume {backups, blocks, new_blocks}.

    Lines whose replica prefix was trimmed by the log pipeline still count toward the totals;
    they are attributed to "unattributed" rather than dropped, because losing them would
    understate spend and understating is the failure mode that matters here.
    """
    vols = {}
    for _, line in rows:
        m = BACKUP_BLOCKS_RE.search(line)
        if not m:
            continue
        v = BACKUP_VOLUME_RE.search(line)
        name = v.group(1) if v else "unattributed"
        entry = vols.setdefault(name, {"backups": 0, "blocks": 0, "new_blocks": 0})
        entry["backups"] += 1
        entry["blocks"] += int(m.group(1))
        entry["new_blocks"] += int(m.group(2))
    return vols


def split_spend_by_target(vols, targets, b2_target=B2_BACKUP_TARGET_NAME):
    """Partition `parse_backup_spend`'s volumes into (b2, other, unknown) by backup target.

    `other` is keyed by target name, because a third store added later must not silently land
    in B2's figure. `unknown` holds every volume the join could not resolve: the "unattributed"
    pseudo-volume, a volume deleted since its backup ran, and every volume at once when the
    `kubectl get volumes.longhorn.io` behind `targets` failed and returned {}.

    The join reads each volume's CURRENT target. A volume moved between tiers inside the window
    is attributed wholly to its new target, so the error is bounded to that one volume.

    `longhorn_blocks.volume_tier_census` warns that the recurring-job GROUP decides the tier and
    `spec.backupTargetName` does not, because `default` is the default name and 18 volumes no job
    backs up report it. That trap cannot bite here: the only volumes in `vols` are ones that
    emitted a backup log line, so an unbacked volume never reaches this function.
    """
    b2, other, unknown = {}, {}, {}
    for vol, v in vols.items():
        target = targets.get(vol)
        if not target:
            unknown[vol] = v
        elif target == b2_target:
            b2[vol] = v
        else:
            other.setdefault(target, {})[vol] = v
    return b2, other, unknown


def _sum(vols, field):
    return sum(v[field] for v in vols.values())


def format_backup_spend(vols, window, names=None, ledger=None, targets=None):
    """Render measured backup spend alongside recorded maintenance spend.

    Never exits non-zero: this is a meter, not a gate. The two halves are printed separately and
    deliberately NOT summed — the backup figure spans --since while the ledger covers the UTC day
    B2's counters reset on, so a combined total would match neither window.

    `targets` is `{volume: backupTargetName}`. Only the B2-target volumes reach the "Class B
    measured" figure, which is the number an operator reads against B2's daily cap; the rest are
    printed as their own subtotals. Volumes the join cannot resolve are counted INTO the B2
    figure, which is the opposite bias to `parse_backup_deletions`: that path writes the ledger,
    so it declines rather than over-charge, while this one only prints, so it over-reports rather
    than print 0 Class B during the cap incident it exists for.
    """
    names = names or {}
    b2, other, unknown = split_spend_by_target(vols, targets or {})
    rows = []
    if vols:
        rows.append(
            "%-24s %-8s %8s %8s %10s"
            % ("PVC", "TARGET", "BACKUPS", "BLOCKS", "UPLOADED")
        )
        for vol in sorted(vols, key=lambda k: -vols[k]["blocks"]):
            v = vols[vol]
            rows.append(
                "%-24s %-8s %8d %8d %10d"
                % (
                    names.get(vol, vol)[:24],
                    ((targets or {}).get(vol) or "?")[:8],
                    v["backups"],
                    v["blocks"],
                    v["new_blocks"],
                )
            )
        rows.append("")
        rows.append(
            "backups over %s: %d Class B measured against B2, %d blocks uploaded "
            "(Class A, unmetered)"
            % (
                window,
                _sum(b2, "blocks") + _sum(unknown, "blocks"),
                _sum(b2, "new_blocks") + _sum(unknown, "new_blocks"),
            )
        )
        for target in sorted(other):
            rows.append(
                "  %d Class B on the %s target, excluded — its caps are not B2's"
                % (_sum(other[target], "blocks"), target)
            )
        # Gated on the block count, not on `unknown` being non-empty: the "unattributed" row
        # is routinely present with 0 blocks, and a line reporting 0 costs a reread.
        if _sum(unknown, "blocks"):
            rows.append(
                "  includes %d Class B from %d volume(s) whose target did not resolve, counted "
                "against B2 because under-reporting the cap is the worse error"
                % (_sum(unknown, "blocks"), len(unknown))
            )
    else:
        rows.append(
            f"no backups logged in the last {window} — widen --since, or nothing ran"
        )

    ledger = ledger or {}
    rows.append("")
    if ledger:
        rows.append("maintenance recorded today (UTC), from the ledger:")
        for tool in sorted(ledger, key=lambda k: -ledger[k]["class_c"]):
            t = ledger[tool]
            rows.append(
                "  %-22s %3d run(s) %6d Class B %6d Class C"
                % (tool[:22], t["runs"], t["class_b"], t["class_c"])
            )
        rows.append(
            "  %-22s %10d Class B %6d Class C"
            % (
                "TOTAL",
                sum(t["class_b"] for t in ledger.values()),
                sum(t["class_c"] for t in ledger.values()),
            )
        )
    else:
        rows.append(
            "no maintenance recorded today — nothing has written the ledger yet"
        )

    rows.append("")
    rows.append(
        "Still unrecorded: Longhorn's own metadata reads (Backup-CR pulls, target syncs) and "
        "the monitor's B2 probes. B2 publishes no counter to reconcile against, so the console's "
        "Caps & Alerts page remains the only ground truth."
    )
    return "\n".join(rows)
