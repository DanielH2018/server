"""Hardware and host verdicts for check.py — the UPS, SMART/scrutiny, and the Pi.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge/parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

The partial-absence handling in `ups_health` is the subtle part: a missing arm can mean the
whole scrape is down, the NUT server dropped, or one entity was renamed, and only the last
should page here. The other two belong to monitors that own that fault.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

from bridge.parsing import parse_rfc3339


def scrutiny_freshness(
    summary: dict | None, max_age_h: float, now: datetime | None = None
) -> tuple[bool, str]:
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


def _scrutiny_status_desc(status: int) -> str:
    """Human-readable reason for a non-zero Scrutiny device_status (a bitwise enum)."""
    if not isinstance(status, int):
        return "device_status %s" % status
    reasons = []
    if status & 1:
        reasons.append("SMART self-assessment FAILED")
    if status & 2:
        reasons.append("Scrutiny attribute threshold breached")
    return ", ".join(reasons) or ("device_status %s" % status)


def scrutiny_health(summary: dict | None, temp_max: float = 0) -> tuple[bool, str]:
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


def scrutiny_device_wear(details: dict | None) -> float | None:
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


def scrutiny_wear_verdict(
    devices: list[tuple[str, float | None]], wear_max: float
) -> tuple[bool, str]:
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


def ups_health(
    charge_pct: float | None,
    runtime_s: float | None,
    replace_battery: float | None,
    charge_min_pct: float,
    runtime_min_s: float,
) -> tuple[bool, str]:
    """Pure: is the UPS battery healthy? Returns (ok, msg).

    Judged on charge (%), estimated runtime (s) and the replace-battery verdict (0/1).

    Any value may be None (that metric absent) — only present arms are judged, and the caller
    handles the all-absent / partial-absence cases. A low charge means an active deep discharge on
    battery; a low runtime means an aged battery whose full-charge runway has decayed OR a discharge
    nearing shutdown; replace_battery>0 is the UPS's OWN self-test verdict (NUT RB flag), which can
    trip while charge/runtime still read fine — the earliest replace-the-battery signal. Strict `<`,
    so a value exactly at the floor is still ok.
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


def pi_pressure(
    load_json: dict | None,
    mem_json: dict | None,
    fs_json: list | None,
    load_max: float,
    mem_min_mb: float,
    disk_max_pct: float,
) -> tuple[bool, str]:
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
# therefore expected to be serving; `restarting`, `created`, `paused` and `exited` are another
# monitor's fault and are named in the message rather than diagnosed here.
PI_UP_STATUSES = ("running", "healthy")


def pi_ports_verdict(
    dead: list[tuple[str, int]], checked: int, containers_json: list | None = None
) -> tuple[bool, str]:
    """Pure: judge the Pi's published ports, attributing a dead one to its container.

    After a daniel-pi reboot a container can come back attached to NO Docker network while
    still reporting `Up (healthy)` — its healthcheck curls loopback inside its own netns, so
    Docker, autoheal and every healthcheck-based signal read green. The observable harm is
    that the published port stops listening, and only a recreate restores it; autoheal's
    restart loop re-enters the same empty sandbox and can never recover it.

    `dead` is the list of (name, port) pairs that failed a TCP connect, `checked` how many
    were probed. The TCP probe is the primary signal deliberately: glances' /api/4/containers
    endpoint costs 4.4s on an idle Pi and has been measured timing out at 10s, so polling it
    every cycle would leave the arm failing open most of the time — inert behind a green
    monitor. A connect to a port that is either listening or not is cheap, unambiguous, and
    is the thing the operator actually cares about.

    `containers_json` is fetched ONLY when something is already dead, and is used to say WHY:

    - up, and Docker reports no `->` mapping  -> detached, and a restart will not fix it
    - up, and Docker does report a mapping    -> publishing but unreachable (bind or firewall)
    - present but not up                      -> ordinary down, named with its status
    - absent from the payload                 -> the container is gone
    - None (the fetch failed, or was slow)    -> cause unknown, and the port is still dead

    That last row is why the attribution fetch cannot make this arm vacuous: a failed fetch
    downgrades the diagnosis, never the verdict.
    """
    if not dead:
        return True, "%d pi port(s) listening" % checked
    by_name = {}
    for c in containers_json or []:
        name = c.get("name")
        if name:
            by_name[name] = c
    detached, other = [], []
    for name, port in dead:
        where = "%s:%d" % (name, port)
        c = by_name.get(name)
        if containers_json is None:
            other.append("%s (cause unknown)" % where)
        elif c is None:
            other.append("%s (container absent)" % where)
        elif (c.get("status") or "").lower() not in PI_UP_STATUSES:
            other.append("%s (%s)" % (where, c.get("status") or "unknown status"))
        elif not any("->" in s for s in (c.get("ports") or "").split(",")):
            detached.append(where)
        else:
            other.append("%s (publishing but unreachable)" % where)
    parts = []
    if detached:
        parts.append(
            "%d pi container(s) up with no published ports, RECREATE (a restart cannot "
            "fix it): %s" % (len(detached), ", ".join(detached))
        )
    if other:
        parts.append("%d pi port(s) not listening: %s" % (len(other), ", ".join(other)))
    return False, "; ".join(parts)


def _hwmon_sensor_key(labels: dict) -> tuple[str, str, str]:
    """Identity of one hwmon sensor: the same triple in the temp and the max vector."""
    return (
        labels.get("instance", "?"),
        labels.get("chip", "?"),
        labels.get("sensor", "?"),
    )


def _hwmon_chip_excluded(labels: dict, exclude_chip: str) -> bool:
    """Whether this series' chip is one check_scrutiny owns rather than check_host_temp.

    ONE predicate, called by both hwmon_temp_limits and hwmon_included_series. The host-coverage
    floor counts origins over the series that survive exclusion, so a second exclusion added to
    only one of the two would leave a host whose sensors are all dropped still counting toward
    the floor — coverage that reads green for a host nothing is checking.
    """
    return bool(exclude_chip) and exclude_chip in labels.get("chip", "?")


def hwmon_included_series(
    temps: list[tuple[dict, float]] | None, exclude_chip: str
) -> list[tuple[dict, float]]:
    """Pure: the scraped temp series check_host_temp actually covers, exclusions applied.

    Same shape in as out — [(labels, value), ...] — so the result feeds _host_origin_shortfall,
    which reads the `origin` label off each entry.
    """
    return [
        (la, v) for la, v in temps or [] if not _hwmon_chip_excluded(la, exclude_chip)
    ]


# How many hot sensors the message names before collapsing the rest to a count. The message lands
# in a Kuma tile and a Discord line, so an estate-wide thermal event must not be a wall of text —
# but the tail is counted rather than dropped.
_HWMON_MAX_LISTED = 5


def hwmon_name_maps(
    chip_names: list[tuple[dict, float]] | None,
    sensor_labels: list[tuple[dict, float]] | None,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str, str], str]]:
    """Pure: the two lookups that turn a sysfs path into the name an operator reads.

    node-exporter publishes them as separate metrics — `node_hwmon_chip_names` carries
    `chip_name` per (instance, chip), `node_hwmon_sensor_label` carries `label` per sensor — so
    `daniel-box/pci0000:00_0000:00:18_3/temp1` is really `daniel-box k10temp/Tctl`.

    Returns (chip_name_by_pair, sensor_label_by_triple). Both are PARTIAL: measured live
    2026-09-01, 10 of 21 series carry a sensor label and a chip with no `chip_name` row is
    normal, so every caller degrades to the raw sysfs component.
    """
    chips = {}
    for labels, _value in chip_names or []:
        name = labels.get("chip_name")
        if name:
            chips[(labels.get("instance", "?"), labels.get("chip", "?"))] = name
    sensors = {}
    for labels, _value in sensor_labels or []:
        label = labels.get("label")
        if label:
            sensors[_hwmon_sensor_key(labels)] = label
    return chips, sensors


def hwmon_display_name(
    key: tuple[str, str, str],
    names: tuple[dict[tuple[str, str], str], dict[tuple[str, str, str], str]] | None,
) -> str:
    """Pure: "<host> <chip>/<sensor>" for one sensor triple, each half named where it can be.

    Degrades PER COMPONENT — a chip with a name whose sensor has none still reads
    "daniel-box acpitz/temp0" — so a missing name costs readability, never identity.
    """
    instance, chip, sensor = key
    chips, sensors = names or ({}, {})
    return "%s %s/%s" % (
        instance,
        chips.get((instance, chip)) or chip,
        sensors.get(key) or sensor,
    )


def hwmon_temp_limits(
    temps: list[tuple[dict, float]] | None,
    maxes: list[tuple[dict, float]] | None,
    ratio: float,
    fallback_c: float,
    min_plausible: float,
    max_plausible: float,
    exclude_chip: str,
    names: tuple[dict[tuple[str, str], str], dict[tuple[str, str, str], str]]
    | None = None,
    crits: Sequence[tuple[dict, float]] = (),
) -> list[tuple[str, float, float, str]]:
    """Pure: assign every scraped sensor a temperature limit.

    Returns a list of (label, temp, limit, basis) with basis "declared" or "fallback".

    Exhaustive by construction — a sensor either has a plausible declared limit or takes the
    fallback, so no scraped sensor is ever left without one. That is the property worth holding:
    a check that silently covers a subset reads green for the rest forever.

    Two declared sources are read — `node_hwmon_temp_max_celsius` (`maxes`) and
    `node_hwmon_temp_crit_celsius` (`crits`) — because a driver need not publish either, and need
    not publish both: k10temp on daniel-box's Ryzen 7 8845HS publishes NEITHER for Tctl (verified
    against `/sys/class/hwmon/hwmon2/` 2026-09-03 — only `temp1_input` and `temp1_label` exist),
    while daniel-server's NVMe controller publishes both at DIFFERENT values (max 85.85, crit
    86.85, measured 2026-09-03). **max wins when a sensor declares a plausible value for both**:
    the hwmon convention (and that measured pair) has `max`/"high" as the earlier, more
    conservative advisory threshold and `crit` as the later shutdown point, so ratioing against
    `crit` would page closer to hardware failure than the existing 90% used against `max`. `crit`
    is used only when `max` is absent or implausible for that sensor — a declared limit a driver
    that skips `max` still gives is a better bound than the flat fallback. Where neither is
    plausible, the sensor takes the fallback.

    A declared value outside (min_plausible, max_plausible] is treated as ABSENT, not as a limit.
    Some NVMe controllers report 65261.85 for "no max declared" and a ratio of that never fires —
    the same sentinel check applies to `crits`.

    `names` is the hwmon_name_maps pair; omitting it labels sensors by their raw sysfs path.
    """
    declared = {}
    for labels, value in crits or []:
        if min_plausible < value <= max_plausible:
            declared[_hwmon_sensor_key(labels)] = value
    for labels, value in maxes or []:
        if min_plausible < value <= max_plausible:
            # Processed after crits and unconditionally overwrites: max is the more
            # conservative declared source, so it wins whenever it is itself plausible.
            declared[_hwmon_sensor_key(labels)] = value
    out = []
    for labels, temp in hwmon_included_series(temps, exclude_chip):
        key = _hwmon_sensor_key(labels)
        label = hwmon_display_name(key, names)
        cap = declared.get(key)
        if cap is None:
            # DECIDED: k10temp subtracts no Tctl offset on daniel-box, so there is no lower
            # series to prefer over the Tctl this arm caps at the flat fallback (issue #1003;
            # #995 established only that k10temp declares no max/crit here). The driver sets
            # `temp_offset` ONLY on a `tctl_offset_table` hit and reports
            # `Tdie = get_raw_temp() - temp_offset`. Read out of the module this host actually
            # runs — `strings` over
            # /lib/modules/6.8.0-138-generic/kernel/drivers/hwmon/k10temp.ko.zst, confirmed
            # against drivers/hwmon/k10temp.c at tag v6.8 — that table holds six family-0x17
            # SKUs (Ryzen 1600X/1700X/1800X/2700X, Threadripper 19xx/29xx). Both halves of its
            # match fail for `AMD Ryzen 7 8845HS w/ Radeon 780M Graphics` (family 25, model
            # 117): the family is not 0x17, and no entry string is a substring of that model id.
            # So `temp_offset` stays 0, and a Tdie here would be NUMERICALLY IDENTICAL to Tctl —
            # reading it would be a no-op on the same number, not a correction. Live sysfs
            # agrees: hwmon2 carries temp1_input and temp1_label (Tctl) alone, no Tdie, no Tccd,
            # no max, no crit. This settles the offset and NOT the number: 85C is the
            # estate-wide default rather than an 8845HS rating, and this sensor sits above it in
            # ordinary use (90.25C read live 2026-09-05). That residual is #1158.
            out.append((label, temp, fallback_c, "fallback"))
        else:
            out.append((label, temp, cap * ratio, "declared"))
    return out


def hwmon_temp_verdict(limits: list[tuple[str, float, float, str]]) -> tuple[bool, str]:
    """Pure: (ok, msg) over the output of hwmon_temp_limits.

    An EMPTY list is not ok. Zero sensors means the hwmon collector stopped scraping, which is
    exactly the state in which a "nothing is too hot" verdict would be a lie.

    The breach leads the message and the coverage tally trails it. Until 2026-09-01 the tally
    sat INSIDE the breach sentence — "1 of 16 sensor(s): 5 by declared max, 11 by fallback OVER
    limit: ..." — so the reader crossed two counts that say nothing about the hot sensor before
    reaching the one that does. Each hot sensor also names the arm that set its limit, because
    the two want different responses: a declared breach is the hardware calling itself too hot,
    while a fallback breach means only that this chip declares no usable max or crit and 85C may
    not suit it — confirmed 2026-09-03 for daniel-box's k10temp: reading
    `/sys/class/hwmon/hwmon2/` directly shows only `temp1_input` and `temp1_label` (Tctl) exist,
    no `temp1_max` or `temp1_crit` file at all. There is no offset to correct for: the kernel
    subtracts none on this chip, so a Tdie would carry the same number as the Tctl already read —
    see the `DECIDED:` marker in hwmon_temp_limits. A "fallback" breach on that sensor is legible
    as "this chip rates nothing", not as "this chip is over its own rating"; whether 85C is the
    right number for a Ryzen 7 8845HS stays an open question this check cannot answer (#1158).
    """
    if not limits:
        return False, "no hwmon temperature sensors scraped (collector blind?)"
    hot = [(la, t, li, b) for la, t, li, b in limits if t >= li]
    n_declared = sum(1 for _la, _t, _li, b in limits if b == "declared")
    n_fallback = len(limits) - n_declared
    coverage = "%d sensors checked, %d by declared limit, %d by fallback" % (
        len(limits),
        n_declared,
        n_fallback,
    )
    if not hot:
        return True, "all below limit; %s" % coverage
    hot.sort(key=lambda x: x[1] - x[2], reverse=True)
    shown = hot[:_HWMON_MAX_LISTED]
    desc = ", ".join(
        "%s %.1fC over its %.1fC %s limit" % (la, t, li, b) for la, t, li, b in shown
    )
    if len(hot) > len(shown):
        desc += ", +%d more" % (len(hot) - len(shown))
    return False, "%d of %d sensors over limit: %s; %s" % (
        len(hot),
        len(limits),
        desc,
        coverage,
    )


def speedtest_verdict(
    row: dict | None, min_mbps: float, max_age_h: float, now: datetime | None = None
) -> tuple[bool, str]:
    """Pure: judge the newest speedtest-tracker result row. (ok, msg).

    `row` is one element of /api/v1/results' `data`, or None when the app returned no rows at
    all.

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
        return (
            False,
            "last run was %.1fh ago (> %gh) — the 6-hourly schedule has stopped"
            % (
                age_h,
                max_age_h,
            ),
        )

    bits = row.get("download_bits")
    if bits is None:
        return False, "last run completed but recorded no download figure"
    mbps = float(bits) / 1e6
    server = ((row.get("data") or {}).get("server") or {}).get(
        "name"
    ) or "unknown server"
    if mbps < min_mbps:
        return False, "download %.1f Mbps (< %g) via %s — %.1fh ago" % (
            mbps,
            min_mbps,
            server,
            age_h,
        )
    return True, "download %.1f Mbps via %s, %.1fh ago" % (mbps, server, age_h)
