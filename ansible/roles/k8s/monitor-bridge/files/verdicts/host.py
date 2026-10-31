"""Host verdicts for check.py — the Pi and the speedtest.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge/parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

The scrutiny arms are NOT here: they sit in `verdicts/host_smart.py`, which this module must not
import (issue #1708).
"""

from datetime import datetime, timezone


def pi_pressure(
    load5_per_core: float | None,
    avail_bytes: float | None,
    disk_used_pct: dict[str, float],
    readonly_by_mount: dict[str, float],
    load_max: float,
    mem_min_mb: float,
    disk_max_pct: float,
) -> tuple[bool, str]:
    """Pure: load per core, available-memory floor, a full or a read-only filesystem on the Pi.

    Fed from the Pi's own node-exporter series on the `node-pi` scrape job (glances until
    2026-09-18, #2004). load5 (not load1) matches the 5-min poll interval and rides out
    single-probe spikes; MemAvailable (not MemFree) is what the kernel can actually reclaim —
    the box thrashes when THAT runs out. `disk_used_pct` is keyed by block device, the SD card
    being the one that matters: a filling SD card is the classic slow Pi death the server-only
    Root Disk check can't see. A missing arm alerts rather than silently passing — a renamed
    series or a blind collector must surface, same principle as the other checks'
    unreachable-source handling.

    `readonly_by_mount` is `node_filesystem_readonly` keyed by mountpoint: the SD card
    remounting its root read-only after an I/O error is the classic Pi failure, and every
    other arm stays green through it — the fill % freezes, open sockets stay open, and
    log2ram keeps syslog flowing from RAM. It leads the message because it is the fault a
    reboot does not come back from. No grace, like the `KubeletCSIMountReadOnly` rule: a read-only
    remount does not self-heal.
    """
    if (
        load5_per_core is None
        or avail_bytes is None
        or not disk_used_pct
        or not readonly_by_mount
    ):
        return (
            False,
            "node-pi series missing load/mem/fs (Pi node_exporter not reporting?)",
        )
    avail_mb = avail_bytes / 1048576.0
    problems = []
    readonly = sorted(mp for mp, ro in readonly_by_mount.items() if ro)
    if readonly:
        problems.append(
            "READ-ONLY %s (SD card remounted after an I/O error?)" % ", ".join(readonly)
        )
    if load5_per_core > load_max:
        problems.append("load5 %.2f/core (> %.2f)" % (load5_per_core, load_max))
    if avail_mb < mem_min_mb:
        problems.append("mem available %.0fMB (< %.0fMB)" % (avail_mb, mem_min_mb))
    for dev, pct in sorted(disk_used_pct.items(), key=lambda dp: -dp[1]):
        if pct > disk_max_pct:
            problems.append("disk %s %.0f%% (> %.0f%%)" % (dev, pct, disk_max_pct))
    if problems:
        return False, "; ".join(problems)
    # The fullest device is named on the healthy path too: the Pi's vfat /boot/firmware sits
    # at 37% while the SD root reads 8%, and a bare "disk 37%" reads as the card.
    fullest, pct = max(disk_used_pct.items(), key=lambda dp: dp[1])
    return True, "load5 %.2f/core, %.0fMB available, disk %s %.0f%%, rw %s" % (
        load5_per_core,
        avail_mb,
        fullest,
        pct,
        ", ".join(sorted(readonly_by_mount)),
    )


def pi_ports_verdict(dead: list[tuple[str, int]], checked: int) -> tuple[bool, str]:
    """Pure: judge the Pi's published ports.

    After a daniel-pi reboot a container can come back attached to NO Docker network while
    still reporting `Up (healthy)` — its healthcheck curls loopback inside its own netns, so
    Docker, autoheal and every healthcheck-based signal read green. The observable harm is
    that the published port stops listening, and only a recreate restores it; autoheal's
    restart loop re-enters the same empty sandbox and can never recover it.

    `dead` is the list of (name, port) pairs that failed a TCP connect, `checked` how many
    were probed. A connect to a port that is either listening or not is cheap, unambiguous,
    and is the thing the operator actually cares about. Until 2026-09-18 a dead port was
    attributed to its container's Docker state through glances' `/api/4/containers`; glances
    retired (#2004) and nothing the cluster can reach serves that view (docker-proxy publishes
    no port), so the message names the port and the two causes, and `ssh daniel-pi docker ps`
    tells them apart.
    """
    if not dead:
        return True, "%d pi port(s) listening" % checked
    return False, (
        "%d pi port(s) not listening: %s (a container up with no port mapping after a "
        "reboot is detached and needs a RECREATE, not a restart)"
        % (len(dead), ", ".join("%s:%d" % (name, port) for name, port in dead))
    )


def _sub_floor(row: dict | None, min_mbps: float) -> bool:
    """Pure: is `row` a completed result whose download figure is under the floor?

    Anything else — no row, a failed row, a completed row with no figure — is NOT a sub-floor
    result, so it cannot be the second half of a consecutive pair. A failed run writes no row at
    all (see check_speedtest's `# DECIDED:`), and the status and age arms judge such a row on
    their own account.
    """
    if not row or row.get("status") != "completed":
        return False
    bits = row.get("download_bits")
    return bits is not None and float(bits) / 1e6 < min_mbps


def speedtest_verdict(
    row: dict | None,
    min_mbps: float,
    max_age_h: float,
    now: datetime | None = None,
    prev_rows: list | None = None,
    floor_consecutive: int = 2,
) -> tuple[bool, str]:
    """Pure: judge the newest speedtest-tracker result row. (ok, msg).

    `row` is one element of /api/v1/results' `data`, or None when the app returned no rows at
    all. `prev_rows` holds the rows BEFORE it, newest first — the floor arm is the only arm that
    reads them.

    THE FLOOR ARM NEEDS `floor_consecutive` SUB-FLOOR RESULTS, the other arms judge `row` alone
    (#2785). One slow result held the tile red for the full 6h until the next test, three times
    in the 14 days to 2026-09-27 (11.6h in total), and a single Ookla run is not evidence the
    link is slow. Consecutive means consecutive RESULTS, not consecutive bridge cycles: the
    cycles re-read the identical row. A run of results is genuinely new information; a run of
    cycles is not. With fewer results available than the knob asks for — a fresh app, or a
    single row in the window — the arm holds `up` and says so, because it cannot yet see a run.

    THE TIMESTAMP IS UTC DESPITE CARRYING NO OFFSET. /api/v1/results serializes `created_at` as
    a bare "2026-08-24 11:00:00", while /api/speedtest/latest serializes the SAME row as
    "2026-08-24T06:00:00.000000-05:00" — verified against row id 780 on 2026-08-24. The bare
    form is therefore UTC, not the DISPLAY_TIMEZONE local time it resembles, and
    datetime.fromisoformat returns it naive. Attaching UTC explicitly is what keeps the age
    arm from reading five hours off; a naive value compared against an aware `now` raises
    instead, which is the safer of the two failures but still not a verdict.

    Arms run status, then age, then floor, in that order and for that reason: `download_bits`
    is null on a failed row, so a floor comparison ahead of the status arm compares None.
    """
    now = now or datetime.now(timezone.utc)
    if not row:
        return (
            False,
            "speedtest has no results at all — the scheduler has never completed a run",
        )

    status = row.get("status")
    created = row.get("created_at")

    if status != "completed":
        detail = ((row.get("data") or {}).get("message") or "").strip()
        return False, "last run (%s) %s%s" % (
            created or "unknown time",
            status or "has no status",
            " — " + detail if detail else "",
        )

    if not created:
        return False, "last run has no created_at — cannot judge freshness"
    stamp = datetime.fromisoformat(created.strip().replace(" ", "T"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_h = (now - stamp).total_seconds() / 3600
    if age_h > max_age_h:
        # DECIDED: name both hypotheses, assert neither (#1483). A failed Ookla run writes no
        # row and a stopped scheduler writes no row, so this arm cannot tell them apart —
        # evidence in test_speedtest_stale_message_names_both_causes_and_asserts_neither.
        return False, (
            "last run was %.1fh ago (> %gh) — no row written since. Either the scheduler "
            "stopped or the run failed: speedtest-tracker writes no row for a failed run, so "
            "the API cannot tell them apart. The pod log's per-tick line is the discriminator."
            % (age_h, max_age_h)
        )

    bits = row.get("download_bits")
    if bits is None:
        return False, "last run completed but recorded no download figure"
    mbps = float(bits) / 1e6
    server = ((row.get("data") or {}).get("server") or {}).get(
        "name"
    ) or "unknown server"
    if mbps < min_mbps:
        # A contiguous run backwards from the newest result, not a count of sub-floor rows in
        # the window: one slow result either side of a fast one is two incidents, not a run.
        run = 1
        for older in list(prev_rows or [])[: max(0, floor_consecutive - 1)]:
            if not _sub_floor(older, min_mbps):
                break
            run += 1
        if run < floor_consecutive:
            return True, (
                "download %.1f Mbps (< %g) via %s — %.1fh ago; %d/%d sub-floor results, "
                "holding for the next test"
                % (mbps, min_mbps, server, age_h, run, floor_consecutive)
            )
        return False, (
            "download %.1f Mbps (< %g) via %s — %.1fh ago; %d/%d consecutive results under "
            "the floor" % (mbps, min_mbps, server, age_h, run, floor_consecutive)
        )
    return True, "download %.1f Mbps via %s, %.1fh ago" % (mbps, server, age_h)
