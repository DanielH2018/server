"""What one Longhorn retention prune costs B2 in Class C transactions, per weekly shard.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Pure: both functions take
the `path;size` listing `b2_api.b2_longhorn_lines` returns plus the cluster facts
`longhorn_cluster.py` reads, and return text and an exit code. Nothing here runs a command or
imports a sibling, so every budget verdict can be asserted without B2 or a cluster. The one
import is the backup group names from `longhorn_backups`, which runs nothing either.

longhorn.py keeps the `b2-budget` subcommand that drives this.
"""

from lib.longhorn_lib import longhorn_backups

# Retention delete spends Class C, one ListObjects per directory per deleted backup, so a shard's
# daily cost follows its BLOCK count, not changed data.
B2_CLASS_C_DAILY_CAP = 2500
# Headroom for what the model does not count: monitor-bridge's two B2 probes and per-prune extras
# beyond the block walk.
B2_BUDGET_RESERVE = 400


def parse_backup_budget(lines):
    """Per-volume backup count, block count, and the Class C cost of one retention prune.

    Takes the same `path;size` lines as parse_longhorn_listing. A prune costs one
    ListObjects for `blocks/`, one per first-level hash directory, and one per second-level
    directory — so the DIRECTORY counts, not the block count, are the price.
    """
    vols = {}
    for line in lines:
        path = line.strip().rpartition(";")[0]
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:
            continue
        v = vols.setdefault(
            parts[i + 3], {"backups": 0, "blocks": 0, "lv1": set(), "lv2": set()}
        )
        tail = parts[i + 4 :]
        if tail[:1] == ["backups"] and path.endswith(".cfg"):
            v["backups"] += 1
        elif path.endswith(".blk") and tail[:1] == ["blocks"] and len(tail) >= 4:
            v["blocks"] += 1
            v["lv1"].add(tail[1])
            v["lv2"].add((tail[1], tail[2]))
    for v in vols.values():
        v["prune"] = 1 + len(v["lv1"]) + len(v["lv2"])
    return vols


def format_backup_budget(vols, shards, names=None, owners=None, targets=None):
    """Render the per-shard Class C projection; non-zero exit if a shard is over budget.

    `shards` maps volume name to its recurring-job group. A volume with no group never runs a
    backup and never prunes, so it is reported separately rather than charged to a day.
    `targets` maps volume name to its live `spec.backupTargetName`. A daily volume whose
    target is named and is not B2 runs its nightly prune on that target, so its B2 backups
    are stranded, not a nightly B2 cost. A missing or blank target is still charged.
    """
    names = names or {}
    targets = targets or {}
    budget = B2_CLASS_C_DAILY_CAP - B2_BUDGET_RESERVE
    byshard, idle, daily, off_b2 = {}, [], [], []
    for vol, v in vols.items():
        shard = shards.get(vol)
        target = targets.get(vol)
        if shard and shard.startswith(longhorn_backups.WEEKLY_SHARD_PREFIX):
            byshard.setdefault(shard, []).append((vol, v))
        elif shard in (None, longhorn_backups.NO_BACKUP_GROUP):
            idle.append(vol)
        elif target and target != longhorn_backups.B2_TARGET:
            off_b2.append(vol)
        else:
            # A PVC from the `longhorn` StorageClass lands in the DAILY group until the next deploy
            # relabels it: a nightly prune against a weekly budget.
            daily.append(vol)

    rows, over = [], []
    for shard in sorted(byshard):
        members = sorted(byshard[shard], key=lambda kv: -kv[1]["prune"])
        total = sum(v["prune"] for _, v in members)
        # Backups beyond `retain` are STRANDED, not queued for deletion, when the job that
        # produced them is not the job that now selects the volume. STRANDED means "no job will
        # ever prune this", which is NOT the same as "past retain" (`max(0, backups - retain)`).
        # Longhorn enforces retain only when the owning RecurringJob runs against a volume still
        # in its `groups:`, and it counts only ITS OWN backups — so the daily-era backups on a
        # volume that moved to a weekday shard are pruned by nothing, ever, however many backups
        # the volume has in total. For example radarr-config can sit at 4 daily-backup + 1
        # weekly-backup-d2 against retain 4 and delete none, because the weekly job sees 1 of its
        # own. `scripts/backup/longhorn_reap.py backups` is what clears these.
        #
        # Anything the current tier owns is NOT stranded even when it sits past retain, because
        # that job prunes it on its next run. A shard's prune cost does not begin until that job
        # has more than `retain` of its own backups, and until then its blocks only grow.
        # No ownership data means nothing is PROVEN stranded, so claim nothing. Falling back to
        # `backups - retain` gives the wrong number, and this figure is read immediately before
        # someone deletes a backup.
        stranded = (
            0
            if owners is None
            else sum(
                max(0, v["backups"] - owners.get(vol, {}).get(shard, 0))
                for vol, v in members
            )
        )
        flag = "OVER" if total > budget else "ok"
        if total > budget:
            over.append(shard)
        rows.append(
            "%-17s %5d C  %s%s"
            % (
                shard,
                total,
                flag,
                "  (%d stranded backup(s) — see the orphan-backup reaper)" % stranded
                if stranded
                else "",
            )
        )
        for vol, v in members:
            rows.append(
                "    %-22s %5d C  %5d blocks  %2d backups"
                % (names.get(vol, vol)[:22], v["prune"], v["blocks"], v["backups"])
            )
    if idle:
        rows.append(
            "no-backup (never pruned): %s"
            % ", ".join(sorted(names.get(v, v) for v in idle))
        )
    if off_b2:
        rows.append(
            "daily tier off B2, %d stranded backup(s) left on B2 (no job prunes them; "
            "scripts/backup/longhorn_reap.py backups clears them): %s"
            % (
                sum(vols[v]["backups"] for v in off_b2),
                ", ".join(
                    "%s -> %s" % (names.get(v, v), targets[v]) for v in sorted(off_b2)
                ),
            )
        )
    if daily:
        over.append("daily-on-B2")
        rows.append(
            "ON THE DAILY TIER AND ON B2: %s — %d C every night, not once a week. "
            "Route to r2 or move to a weekly shard."
            % (
                ", ".join(sorted(names.get(v, v) for v in daily)),
                sum(vols[v]["prune"] for v in daily),
            )
        )
    rows.append(
        "budget per day: %d Class C (cap %d less %d reserved for the B2 probes and prune overhead)"
        % (budget, B2_CLASS_C_DAILY_CAP, B2_BUDGET_RESERVE)
    )
    if over:
        rows.append("OVER BUDGET: %s — rebalance before the next run" % ", ".join(over))
    return "\n".join(rows), (1 if over else 0)
