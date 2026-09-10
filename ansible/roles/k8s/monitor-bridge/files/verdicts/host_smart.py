"""SMART verdicts for check.py — what scrutiny reports about the drives.

Its own module rather than a section of `verdicts/host.py` for the reason
`verdicts/host_power.py` records: that file had reached the 600-line cap in
`ansible/tests/repo/test_module_length_ratchet.py` exactly, so the next line added to it failed
CI (issue #1708). Splitting the UPS half alone left 39 lines of headroom; this second cut is
what makes that headroom durable.

Three arms, all read by `check_scrutiny` (checks/host_thermal.py): the collector's freshness,
the per-device SMART status and temperature, and NVMe wear. Drive TEMPERATURE belongs here and
not to `hwmon_temp_verdict` — `test_drive_chips_are_left_to_scrutiny` holds that boundary.

Decides; does not fetch. Takes its inputs as arguments and reads no module-level config — see
`bridge/parsing.py`'s header for the rule and why breaking it fails silently rather than loudly.
"""

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
