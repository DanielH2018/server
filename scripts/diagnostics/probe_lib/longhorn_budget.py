"""What one Longhorn retention prune costs B2 in Class C transactions, per weekly shard.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Pure: both functions take
the `path;size` listing `b2_api.b2_longhorn_lines` returns plus the cluster facts
`longhorn_cluster.py` reads, and return text and an exit code. Nothing here runs a command or
imports a sibling, so every budget verdict can be asserted without B2 or a cluster.

longhorn.py keeps the `b2-budget` subcommand that drives this.
"""

# B2's free tier allows 2,500 Class C transactions a day. Longhorn's retention delete is the
# thing that spends them: DeleteDeltaBlockBackup walks the volume's whole block tree with one
# delimited ListObjects per directory (backupstore deltablock.go getBlockNamesForVolume), and it
# runs once per deleted backup. So a shard's daily Class C cost is set by how many BLOCKS its
# volumes hold, not by how much data changed — which is why this is worth watching as volumes
# grow, and why the seventh cap event was not preventable by looking at bytes.
B2_CLASS_C_DAILY_CAP = 2500
# Headroom for everything this model does not count: monitor-bridge's two B2 probes
# (check_b2_reachable, and check_b2_storage's daily listing), and the per-prune extras beyond the
# block walk — the deletion lock check, the backups/ name list, and one cfg GET per retained
# backup. NOT kopia: it was retired with the k3s migration and issues no B2 traffic at all. Only
# the `kopia_b2_*` SOPS key names survive, and they are Longhorn's credentials now.
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


def format_backup_budget(vols, shards, names=None, retain=2, owners=None):
    """Render the per-shard Class C projection; non-zero exit if a shard is over budget.

    `shards` maps volume name to its recurring-job group. A volume with no group never runs a
    backup and never prunes, so it is reported separately rather than charged to a day.
    """
    names = names or {}
    budget = B2_CLASS_C_DAILY_CAP - B2_BUDGET_RESERVE
    byshard, idle, daily = {}, [], []
    for vol, v in vols.items():
        shard = shards.get(vol)
        if shard and shard.startswith("weekly-backup-"):
            byshard.setdefault(shard, []).append((vol, v))
        elif shard in (None, "no-backup"):
            idle.append(vol)
        else:
            # A PVC provisioned from the `longhorn` StorageClass lands in `default` — the DAILY
            # group — until the next deploy reconciles its label. On B2 that is a prune every
            # night against a budget sized for one a week, so it is the loudest thing here.
            daily.append(vol)

    rows, over = [], []
    for shard in sorted(byshard):
        members = sorted(byshard[shard], key=lambda kv: -kv[1]["prune"])
        total = sum(v["prune"] for _, v in members)
        # Backups beyond `retain` are STRANDED, not queued for deletion. Longhorn enforces
        # retain only when the owning RecurringJob runs against a volume still in its `groups:`,
        # and it counts only ITS OWN backups — so the daily-era backups on a volume that moved to
        # a weekday shard are pruned by nothing, ever. Measured 2026-08-17: radarr-config sat at
        # 4 daily-backup + 1 weekly-backup-d2 against retain 4 and deleted none, because the
        # weekly job saw 1 of its own. `longhorn-reap-orphan-backups.sh` is what clears these.
        #
        # The consequence for this projection: a shard's prune cost does not begin until that
        # job has more than `retain` of its own backups, and until then its blocks only grow.
        # STRANDED means "no job will ever prune this", which is NOT the same as "past retain"
        # — and until 2026-08-19 this line computed the latter, `max(0, backups - retain)`,
        # while the comment above described the former. It under-reported by 4.7x: 7 against a
        # true 33, on the number an operator reads before deciding what to delete.
        #
        # A backup is stranded when the job that produced it is not the job that now selects the
        # volume. Longhorn's retain counts only a job's OWN backups, so a daily-era backup on a
        # volume since moved to a weekday shard is pruned by nothing, ever — regardless of how
        # many backups the volume has in total. Anything the current tier owns is NOT stranded
        # even when it sits past retain, because that job prunes it on its next run.
        # No ownership data means nothing is PROVEN stranded, so claim nothing. Falling back to
        # `backups - retain` is what produced the wrong number, and this figure is read
        # immediately before someone deletes a backup.
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
