"""Hardware and host verdicts for check.py — the UPS, SMART/scrutiny, and the Pi.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge_parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

The partial-absence handling in `ups_health` is the subtle part: a missing arm can mean the
whole scrape is down, the NUT server dropped, or one entity was renamed, and only the last
should page here. The other two belong to monitors that own that fault.
"""

from datetime import datetime, timezone

from bridge_parsing import parse_rfc3339


def scrutiny_freshness(summary, max_age_h, now=None):
    """`summary` is the data.summary dict of scrutiny's /api/summary."""
    now = now or datetime.now(timezone.utc)
    stale, n = [], 0
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        n += 1
        name = dev.get("device_name") or wwn
        cdate = (entry.get("smart") or {}).get("collector_date")
        if not cdate:
            stale.append("%s (no SMART data)" % name)
            continue
        age_h = (now - parse_rfc3339(cdate)).total_seconds() / 3600
        if age_h > max_age_h:
            stale.append("%s (last report %.1fh ago)" % (name, age_h))
    if not n:
        return False, "scrutiny reports no devices (collector never ran?)"
    if stale:
        return False, "stale SMART data: " + ", ".join(stale)
    return True, "%d device(s) reported within %gh" % (n, max_age_h)


def _scrutiny_status_desc(status):
    """Human-readable reason for a non-zero Scrutiny device_status (a bitwise enum)."""
    if not isinstance(status, int):
        return "device_status %s" % status
    reasons = []
    if status & 1:
        reasons.append("SMART self-assessment FAILED")
    if status & 2:
        reasons.append("Scrutiny attribute threshold breached")
    return ", ".join(reasons) or ("device_status %s" % status)


def scrutiny_health(summary, temp_max=0):
    """Pure: any non-archived device reporting a drive failure or over-temp? (ok, msg).

    `summary` is scrutiny's /api/summary data.summary dict. device_status is 0 when the drive
    passes both SMART's own self-assessment AND Scrutiny's attribute thresholds, non-zero on a
    failure — the actual drive-failure signal the freshness check (which only proves the collector
    still reports) can't see. A missing device_status is treated as unknown -> ok (don't false-page
    on an API that omits the field). temp_max > 0 adds a temperature ceiling (°C); 0 disables it.
    """
    failing, hot = [], []
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        name = dev.get("device_name") or wwn
        status = dev.get("device_status")
        if status not in (0, None):
            failing.append("%s (%s)" % (name, _scrutiny_status_desc(status)))
        if temp_max:
            temp = (entry.get("smart") or {}).get("temp")
            if temp is not None and temp > temp_max:
                hot.append("%s (%g°C > %g°C)" % (name, temp, temp_max))
    problems = failing + hot
    if problems:
        return False, "SMART health: " + ", ".join(problems)
    return True, "SMART health ok"


def scrutiny_device_wear(details):
    """Pure: one device's `percentage_used`, or None where the device does not report it.

    `details` is the parsed /api/device/<wwn>/details body. `smart_results` is a history array,
    newest first, so only [0] is read. None is not a fault: `percentage_used` is an NVMe attribute,
    so a SATA disk added later legitimately has none and must not page.
    """
    results = ((details or {}).get("data") or {}).get("smart_results") or []
    if not results:
        return None
    attrs = (results[0] or {}).get("attrs") or {}
    entry = attrs.get("percentage_used")
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return value if isinstance(value, (int, float)) else None


def scrutiny_wear_verdict(devices, wear_max):
    """Pure: (ok, msg) for NVMe endurance. `devices` is a list of (label, percentage_used|None).

    A list rather than a dict because both live drives report `device_name` "nvme0" — one per
    host — so keying by name would collapse them into one entry.

    Unreadable wear reports as INERT and names the drives it is not watching, the shape
    `extended_resource_verdict` uses: a check that cannot read its input must not answer as though
    it did, in either direction. DOWN-on-missing-field would page for every non-NVMe disk.
    """
    if not wear_max:
        return True, "NVMe wear check disabled"
    watched = [(label, used) for label, used in devices if used is not None]
    unwatched = [label for label, used in devices if used is None]
    if not watched:
        return True, (
            "NVMe wear check INERT: no device reports percentage_used; %s unwatched"
            % (", ".join(unwatched) or "no devices")
        )
    worn = [
        "%s (%g%% used > %g%%)" % (label, used, wear_max)
        for label, used in watched
        if used > wear_max
    ]
    if worn:
        return False, "NVMe wear: " + ", ".join(worn)
    msg = "NVMe wear ok (max %g%% used of %g%%)" % (
        max(used for _, used in watched),
        wear_max,
    )
    if unwatched:
        msg += "; no percentage_used from %s (unwatched)" % ", ".join(unwatched)
    return True, msg


def ups_health(charge_pct, runtime_s, replace_battery, charge_min_pct, runtime_min_s):
    """Pure: is the UPS battery healthy given charge (%), estimated runtime (s), and the replace-
    battery verdict (0/1)? (ok, msg).

    Any value may be None (that metric absent) — only present arms are judged, and the caller handles
    the all-absent / partial-absence cases. A low charge means an active deep discharge on battery; a
    low runtime means an aged battery whose full-charge runway has decayed OR a discharge nearing
    shutdown; replace_battery>0 is the UPS's OWN self-test verdict (NUT RB flag), which can trip while
    charge/runtime still read fine — the earliest replace-the-battery signal. Strict `<`, so a value
    exactly at the floor is still ok.
    """
    problems = []
    if charge_pct is not None and charge_pct < charge_min_pct:
        problems.append("battery %.0f%% (< %.0f%%)" % (charge_pct, charge_min_pct))
    if runtime_s is not None and runtime_s < runtime_min_s:
        problems.append(
            "runtime %.1fm (< %.1fm)" % (runtime_s / 60.0, runtime_min_s / 60.0)
        )
    if replace_battery is not None and replace_battery > 0.5:
        problems.append("replace-battery (UPS self-test / RB flag)")
    if problems:
        return False, "; ".join(problems)
    parts = []
    if charge_pct is not None:
        parts.append("battery %.0f%%" % charge_pct)
    if runtime_s is not None:
        parts.append("runtime %.1fm" % (runtime_s / 60.0))
    if replace_battery is not None:
        parts.append("self-test ok")
    return True, ", ".join(parts)


def pi_pressure(load_json, mem_json, fs_json, load_max, mem_min_mb, disk_max_pct):
    """Pure: load per core, available-memory floor, or a full filesystem on the Pi.

    Fed glances /api/4/load, /api/4/mem and /api/4/fs payloads. load5 (not load1)
    matches the 5-min poll interval and rides out single-probe spikes; `available`
    (not `free`) is what the kernel can actually reclaim — the box thrashes when THAT
    runs out. The fs list is glances' *container* view: every entry is a bind-mount
    path, but they're all backed by the SD card device with the HOST usage percent —
    so filesystems are deduped by device_name (a filling SD card is the classic slow
    Pi death the server-only Root Disk check can't see). Missing fields and an empty
    fs list alert rather than silently passing (a glances plugin regression must
    surface, same principle as the other checks' unreachable-source handling).
    """
    cores = load_json.get("cpucore") or 0
    load5 = load_json.get("min5")
    avail = mem_json.get("available")
    devices = {}
    for fs in fs_json or []:
        dev, pct = fs.get("device_name"), fs.get("percent")
        if dev and pct is not None:
            devices[dev] = max(pct, devices.get(dev, 0.0))
    if not cores or load5 is None or avail is None or not devices:
        return False, "glances payload missing load/mem/fs fields"
    per_core = load5 / cores
    avail_mb = avail / 1048576.0
    problems = []
    if per_core > load_max:
        problems.append("load5 %.2f/core (> %.2f)" % (per_core, load_max))
    if avail_mb < mem_min_mb:
        problems.append("mem available %.0fMB (< %.0fMB)" % (avail_mb, mem_min_mb))
    for dev, pct in sorted(devices.items(), key=lambda dp: -dp[1]):
        if pct > disk_max_pct:
            problems.append("disk %s %.0f%% (> %.0f%%)" % (dev, pct, disk_max_pct))
    if problems:
        return False, "; ".join(problems)
    return True, "load5 %.2f/core, %.0fMB available, disk %.0f%%" % (
        per_core,
        avail_mb,
        max(devices.values()),
    )


# glances reports Docker's own status string. Only these two mean the container is up and
# therefore expected to be publishing; `restarting`, `created`, `paused` and `exited` are
# another monitor's fault and are named in the message rather than judged here.
PI_UP_STATUSES = ("running", "healthy")


def pi_detached_containers(containers_json, expected_publishers):
    """Pure: Pi containers that are up but have lost their published ports.

    After a daniel-pi reboot, containers can come back attached to NO Docker network while
    still reporting `Up (healthy)` — the healthcheck passes on loopback, so every existing
    signal reads green. The observable harm is that the published port mappings vanish, and
    only a recreate restores them; autoheal's restart loop structurally cannot.

    `containers_json` is glances' /api/4/containers list. `ports` there is Docker's own
    summary string: a comma-separated mix of published mappings (`61208->61208/tcp`) and
    merely-exposed ports (`61209/tcp`). Presence of `->` in any segment is what distinguishes
    the two, which is why this matches on that arrow and not on a port number or a count —
    a container may publish one mapping or several, and wg-easy publishes both TCP and UDP.

    `expected_publishers` is derived from daniel-pi's `containers_list` (every entry with a
    `port`), so it cannot drift from the inventory. Three Pi containers legitimately publish
    nothing — docker-proxy, autoheal and docker-proxy-lifecycle — and are absent from it by
    construction rather than by an exclusion list someone has to maintain.

    An expected container missing from the payload is reported, not skipped: a rename in
    `containers_list`, or glances' docker plugin failing and returning [], would otherwise
    make this arm silently vacuous while reading green.

    Scope: this arm does NOT judge a stopped container, and it cannot see the full-blackout
    case where the Pi comes back with glances itself detached — glances is one of the
    publishers, so the fetch fails and check_pi_pressure renders down with the error, by a
    different mechanism. What this catches is the partial case: some containers reattached
    and others did not.
    """
    by_name = {}
    for c in containers_json or []:
        name = c.get("name")
        if name:
            by_name[name] = c
    detached, missing, not_up = [], [], []
    for name in expected_publishers:
        c = by_name.get(name)
        if c is None:
            missing.append(name)
            continue
        status = (c.get("status") or "").lower()
        if status not in PI_UP_STATUSES:
            not_up.append("%s=%s" % (name, status or "unknown"))
            continue
        segments = (c.get("ports") or "").split(",")
        if not any("->" in s for s in segments):
            detached.append(name)
    problems = []
    if detached:
        problems.append(
            "%d pi container(s) up with no published ports, recreate needed: %s"
            % (len(detached), ", ".join(sorted(detached)))
        )
    if missing:
        problems.append(
            "%d expected pi container(s) not reported by glances: %s"
            % (len(missing), ", ".join(sorted(missing)))
        )
    if problems:
        return False, "; ".join(problems)
    ok_msg = "%d pi container(s) publishing" % (len(expected_publishers) - len(not_up))
    if not_up:
        ok_msg += " (not up, skipped: %s)" % ", ".join(sorted(not_up))
    return True, ok_msg
