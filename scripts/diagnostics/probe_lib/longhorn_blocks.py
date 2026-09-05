"""Is every B2-tier Longhorn volume on 16 MiB blocks? The census and its verdict.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Pure: the three functions
take a parsed `kubectl get volumes.longhorn.io -o json` document and return the grouping, the
offenders and the rendered verdict. Nothing here runs a command or imports a sibling.

longhorn.py keeps the `longhorn-blocks` subcommand that fetches the document and prints this.
"""

# 16 MiB. The migration's whole point: it cuts B2 prune, backup and restore cost ~8x, and
# `default-backup-block-size` is IMMUTABLE per volume, so only volumes created after the change
# get it. That is why this is a census of live state rather than a setting to read once.
LONGHORN_WEEKLY_BLOCK_BYTES = 16 * 1024 * 1024

_RECURRING_GROUP = "recurring-job-group.longhorn.io"


def volume_tier_census(volumes):
    """Group Longhorn Volume CRs by RecurringJob group, target and backup block size.

    The GROUP decides the tier, not `spec.backupTargetName`. `default` is literally the default
    target name, so every volume that was never moved to another target reports it — including
    the ones no job selects at all. Grouping by target alone therefore reads 18 unbacked
    volumes as members of the B2 tier, which is the same trap
    `seed-backups-do-not-count-as-rotation-coverage` records on the coverage side.
    """
    rows = {}
    for item in (volumes or {}).get("items", []):
        spec = item.get("spec") or {}
        labels = (item.get("metadata") or {}).get("labels") or {}
        groups = sorted(
            name
            for group, sep, name in (key.partition("/") for key in labels)
            if sep and group == _RECURRING_GROUP
        )
        key = (
            ",".join(groups) or "-",
            spec.get("backupTargetName") or "-",
            str(spec.get("backupBlockSize") or "-"),
        )
        rows.setdefault(key, []).append((item.get("metadata") or {}).get("name", "?"))
    return rows


def weekly_volumes_off_block_size(rows, expected=LONGHORN_WEEKLY_BLOCK_BYTES):
    """Names of weekly-shard volumes NOT on the expected block size.

    Only `weekly-backup-*` is asserted. `no-backup` volumes are unconstrained — nothing backs
    them up, so their block size cannot cost anything — and the R2/daily volumes are a recorded
    exception, immutable in place and not worth recreating.
    """
    offenders = []
    for (group, _target, block), names in rows.items():
        if not group.startswith("weekly-backup-"):
            continue
        if block != str(expected):
            offenders.extend(f"{n} ({group}, blockSize={block})" for n in names)
    return sorted(offenders)


def format_block_census(rows, expected=LONGHORN_WEEKLY_BLOCK_BYTES):
    """Render the census, and fail when a weekly-shard volume is off the expected size."""
    lines = []
    for (group, target, block), names in sorted(rows.items()):
        mib = int(block) // (1024 * 1024) if block.isdigit() else "?"
        lines.append(
            f"  group={group:<20} target={target:<8} block={mib}MiB  count={len(names)}"
        )
    offenders = weekly_volumes_off_block_size(rows, expected)
    if offenders:
        lines.append("")
        lines.append(
            f"FAIL: {len(offenders)} weekly-shard volume(s) are not on "
            f"{expected // (1024 * 1024)} MiB blocks. Block size is immutable per volume, so "
            "the fix is migrate_volume_block_size.yml followed by a seed backup:"
        )
        lines.extend(f"    {o}" for o in offenders)
        return "\n".join(lines), 1
    lines.append("")
    lines.append(
        f"OK: every weekly-shard volume is on {expected // (1024 * 1024)} MiB blocks."
    )
    return "\n".join(lines), 0
