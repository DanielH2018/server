"""Health verdicts for the hardware and the hosts: disks, certs, SMART, the UPS, the Pi.

These read a sensor and decide. The decision is the whole test — the HTTP glue is exercised
live by `check.py --once` at deploy time, and the run-loop wiring is in test_check_gates.py.
"""

from datetime import datetime, timezone


import check


def _seq(*values):
    """Return a callable that yields each value on successive calls (like mock side_effect)."""
    it = iter(values)
    return lambda *a, **k: next(it)


def test_disk_under_threshold_is_ok(monkeypatch):
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        check, "prom_vector", lambda q: [({"origin": "daniel-box"}, 50.0)]
    )
    ok, msg = check.check_disk()
    assert ok
    assert "under" in msg


def test_disk_over_threshold_names_mount(monkeypatch):
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        check, "prom_vector", lambda q: [({"origin": "daniel-box"}, 95.0)]
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "/" in msg
    assert "95" in msg


def test_disk_names_the_breaching_host_not_the_healthy_one(monkeypatch):
    """THE BUG THIS PINS (2026-08-15): avail and size were two separate max() queries, so once
    both estates reported into one Prometheus a full disk on one host could be paired with the
    other's size. A per-origin percentage keeps each host's numerator with its own denominator,
    and the alert has to name WHICH host is full to be actionable."""
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        check,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 96.0),
            ({"origin": "daniel-box"}, 24.0),
        ],
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "daniel-server" in msg
    assert "daniel-box" not in msg


def test_disk_groups_by_origin_so_neither_host_is_unwatched(monkeypatch):
    seen = {}
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])

    def fake_vector(promql):
        seen["q"] = promql
        return [({"origin": "daniel-box"}, 10.0)]

    monkeypatch.setattr(check, "prom_vector", fake_vector)
    check.check_disk()
    assert "by (origin)" in seen["q"]
    # The division must be inside the query, so the two series are paired by Prometheus on all
    # their labels rather than by two independent aggregates here.
    assert "node_filesystem_avail_bytes" in seen["q"]
    assert "node_filesystem_size_bytes" in seen["q"]


def test_disk_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(check, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    ok, msg = check.check_disk()
    assert not ok
    assert "unavailable" in msg


def test_mem_names_the_breaching_host(monkeypatch):
    monkeypatch.setattr(
        check,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 92.0),
            ({"origin": "daniel-box"}, 30.0),
        ],
    )
    ok, msg = check.check_mem()
    assert not ok
    assert "daniel-server" in msg


def test_mem_reports_the_worst_host_when_all_are_healthy(monkeypatch):
    monkeypatch.setattr(
        check,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 41.0),
            ({"origin": "daniel-box"}, 63.0),
        ],
    )
    ok, msg = check.check_mem()
    assert ok
    assert "63" in msg


def test_mem_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    ok, msg = check.check_mem()
    assert not ok
    assert "unavailable" in msg


def test_cert_valid_is_ok(monkeypatch):
    # default CERT_MIN_DAYS=14; 30 days left -> ok
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: 30.0)
    ok, msg = check.check_cert()
    assert ok
    assert "valid" in msg


def test_cert_expiring_alerts(monkeypatch):
    # 5 days left < 14 -> down
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: 5.0)
    ok, msg = check.check_cert()
    assert not ok
    assert "expires" in msg


def test_cert_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: None)
    ok, msg = check.check_cert()
    assert not ok
    assert "unavailable" in msg


# ── scrutiny SMART-data freshness (collector runs daily; web API holds last report) ──


def _summary(*entries):
    return {e["device"]["wwn"]: e for e in entries}


def _dev(wwn, name, collector_date=None, archived=False, device_status=None, temp=None):
    dev = {"wwn": wwn, "device_name": name, "archived": archived}
    if device_status is not None:
        dev["device_status"] = device_status
    smart = {}
    if collector_date:
        smart["collector_date"] = collector_date
    if temp is not None:
        smart["temp"] = temp
    return {"device": dev, "smart": smart or None}


def test_scrutiny_fresh_device_is_ok():
    s = _summary(_dev("w1", "nvme0", "2026-06-06T06:00:00Z"))
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert ok
    assert "1 device" in msg


def test_scrutiny_stale_device_is_named():
    s = _summary(
        _dev("w1", "nvme0", "2026-06-04T06:00:00Z"),
        _dev("w2", "sda", "2026-06-06T06:00:00Z"),
    )
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert not ok
    assert "nvme0" in msg and "sda" not in msg


def test_scrutiny_no_smart_data_is_down():
    s = _summary(_dev("w1", "nvme0"))
    ok, msg = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert not ok
    assert "no SMART data" in msg


def test_scrutiny_archived_device_is_skipped():
    s = _summary(
        _dev("w1", "nvme0", "2026-06-06T06:00:00Z"),
        _dev("w2", "old-disk", "2020-01-01T00:00:00Z", archived=True),
    )
    ok, _ = check.scrutiny_freshness(
        s, 26, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert ok


def test_scrutiny_no_devices_is_down():
    ok, msg = check.scrutiny_freshness({}, 26)
    assert not ok
    assert "no devices" in msg


# ── scrutiny SMART health (device_status != 0 = a failing drive; freshness alone can't see it) ──


def test_scrutiny_passing_device_is_healthy():
    s = _summary(_dev("w1", "nvme0", device_status=0))
    ok, msg = check.scrutiny_health(s)
    assert ok
    assert "ok" in msg


def test_scrutiny_failed_smart_is_named():
    s = _summary(
        _dev("w1", "nvme0", device_status=1),
        _dev("w2", "sda", device_status=0),
    )
    ok, msg = check.scrutiny_health(s)
    assert not ok
    assert "nvme0" in msg and "SMART self-assessment FAILED" in msg
    assert "sda" not in msg


def test_scrutiny_failed_threshold_is_named():
    s = _summary(_dev("w1", "nvme0", device_status=2))
    ok, msg = check.scrutiny_health(s)
    assert not ok
    assert "attribute threshold breached" in msg


def test_scrutiny_missing_device_status_is_ok():
    # An API that omits device_status must not false-page.
    s = _summary(_dev("w1", "nvme0", "2026-06-06T06:00:00Z"))
    ok, _ = check.scrutiny_health(s)
    assert ok


def test_scrutiny_archived_failing_device_is_skipped():
    s = _summary(_dev("w1", "old-disk", device_status=1, archived=True))
    ok, _ = check.scrutiny_health(s)
    assert ok


def test_scrutiny_temp_ceiling_flags_only_when_enabled():
    s = _summary(_dev("w1", "nvme0", device_status=0, temp=70))
    assert check.scrutiny_health(s, temp_max=0)[0]  # disabled -> ok
    ok, msg = check.scrutiny_health(s, temp_max=60)
    assert not ok
    assert "70" in msg and "60" in msg


# ── scrutiny NVMe wear (percentage_used ships with thresh=100, so device_status can't warn) ──


def _details(attrs=None, results=True):
    """The shape of /api/device/<wwn>/details, trimmed to what scrutiny_device_wear reads."""
    if not results:
        return {"data": {"smart_results": []}}
    return {"data": {"smart_results": [{"attrs": attrs or {}}]}}


def _attr(value):
    return {"percentage_used": {"attribute_id": "percentage_used", "value": value}}


def test_scrutiny_device_wear_reads_newest_result():
    d = {"data": {"smart_results": [{"attrs": _attr(7)}, {"attrs": _attr(3)}]}}
    assert check.scrutiny_device_wear(d) == 7


def test_scrutiny_device_wear_missing_field_is_none():
    assert (
        check.scrutiny_device_wear(_details(attrs={"temperature": {"value": 47}}))
        is None
    )
    assert check.scrutiny_device_wear(_details(results=False)) is None
    assert check.scrutiny_device_wear({}) is None
    assert check.scrutiny_device_wear(None) is None


def test_scrutiny_device_wear_non_numeric_is_none():
    assert check.scrutiny_device_wear(_details(attrs=_attr("n/a"))) is None


def test_scrutiny_wear_under_ceiling_is_ok():
    ok, msg = check.scrutiny_wear_verdict([("nvme0 (SHPP41-500GM)", 7)], 80)
    assert ok
    assert "7% used of 80%" in msg


def test_scrutiny_wear_over_ceiling_is_down():
    ok, msg = check.scrutiny_wear_verdict(
        [("nvme0 (SHPP41-500GM)", 85), ("nvme0 (CT1000E100SSD8)", 0)], 80
    )
    assert not ok
    assert "SHPP41-500GM" in msg and "85% used > 80%" in msg
    assert "CT1000E100SSD8" not in msg


def test_scrutiny_wear_ceiling_disabled_never_fires():
    ok, msg = check.scrutiny_wear_verdict([("nvme0", 99)], 0)
    assert ok
    assert "disabled" in msg


def test_scrutiny_wear_unreadable_is_inert_not_ok():
    # The recorded failure class: a check that cannot read its input must SAY so, not report the
    # healthy meaning. Passing is right (percentage_used is NVMe-only, so a SATA disk has none),
    # but the message has to name what is unwatched.
    ok, msg = check.scrutiny_wear_verdict([("sda (WD40EFRX)", None)], 80)
    assert ok
    assert "INERT" in msg and "sda (WD40EFRX)" in msg


def test_scrutiny_wear_names_partially_unwatched_devices():
    ok, msg = check.scrutiny_wear_verdict([("nvme0", 7), ("sda", None)], 80)
    assert ok
    assert "INERT" not in msg
    assert "7% used" in msg and "sda (unwatched)" in msg


def test_scrutiny_wear_no_devices_is_inert():
    ok, msg = check.scrutiny_wear_verdict([], 80)
    assert ok
    assert "INERT" in msg


def test_scrutiny_wear_devices_skips_archived_and_labels_by_model(monkeypatch):
    # Both live drives report device_name "nvme0", one per host — the label has to carry the model
    # or the two are indistinguishable in the alert.
    fetched = []

    def fake_get_json(url):
        fetched.append(url)
        return _details(attrs=_attr(7))

    monkeypatch.setattr(check, "_get_json", fake_get_json)
    monkeypatch.setattr(check, "SCRUTINY_URL", "http://scrutiny:8080")
    summary = {
        "w1": {"device": {"device_name": "nvme0", "model_name": "SHPP41-500GM"}},
        "w2": {"device": {"device_name": "sda", "archived": True}},
    }
    devices = check.scrutiny_wear_devices(summary)
    assert devices == [("nvme0 (SHPP41-500GM)", 7)]
    assert fetched == ["http://scrutiny:8080/api/device/w1/details"]


# ── ups (battery health via HA's Prometheus-scraped UPS sensors) ─────────────


def test_ups_health_ok():
    ok, msg = check.ups_health(100, 900, 0, 50, 300)
    assert ok
    assert "battery 100%" in msg and "runtime 15.0m" in msg and "self-test ok" in msg


def test_ups_health_low_charge_is_named():
    ok, msg = check.ups_health(30, 900, 0, 50, 300)
    assert not ok
    assert "battery 30%" in msg and "runtime" not in msg


def test_ups_health_low_runtime_is_named():
    ok, msg = check.ups_health(100, 120, 0, 50, 300)
    assert not ok
    assert "runtime 2.0m" in msg and "battery" not in msg


def test_ups_health_both_breaches_named():
    ok, msg = check.ups_health(20, 60, 0, 50, 300)
    assert not ok
    assert "battery 20%" in msg and "runtime 1.0m" in msg


def test_ups_health_replace_battery_pages_even_with_good_runway():
    # The UPS's own RB self-test verdict trips even while charge/runtime read fine — earliest signal.
    ok, msg = check.ups_health(100, 900, 1, 50, 300)
    assert not ok
    assert "replace-battery" in msg


def test_ups_health_at_threshold_is_ok():
    # strict `<`, so exactly at the floor is fine
    assert check.ups_health(50, 300, 0, 50, 300)[0]


def test_ups_health_absent_arm_is_skipped():
    # only runtime present and low -> pages on runtime alone; the other arms are ignored
    ok, msg = check.ups_health(None, 120, None, 50, 300)
    assert not ok
    assert "runtime" in msg and "battery" not in msg


def _ups_scalars(monkeypatch, charge, runtime, replace=0.0, ha_up=None):
    def fake(q):
        if q == check.UPS_CHARGE_QUERY:
            return charge
        if q == check.UPS_RUNTIME_QUERY:
            return runtime
        if q == check.UPS_REPLACE_QUERY:
            return replace
        if q == check.UPS_HA_UP_QUERY:
            return ha_up
        return None

    monkeypatch.setattr(check, "prom_scalar", fake)


def test_check_ups_healthy_is_up(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900)
    ok, msg = check.check_ups()
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(monkeypatch):
    # HA scrape down (ha_up None via the fake) -> all arms absent defers to Scrape Targets.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None)
    ok, msg = check.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_all_absent_but_ha_scraping_pages(monkeypatch):
    # Every UPS entity renamed/removed at once while HA keeps scraping (up{home-assistant}==1):
    # Scrape Targets can't see it, so the old all-absent defer silently unmonitored the UPS. Now it
    # pages through the streak (naming the missing arms) instead of deferring.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=1.0)
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = check.check_ups()
    assert not ok2 and "absent" in msg2
    assert check._ups_down_streak == 2


def test_check_ups_all_absent_ha_down_still_defers(monkeypatch):
    # HA scrape affirmatively down (up==0) with all arms absent -> still defer (Scrape Targets owns
    # the HA-source outage); the up-gate only flips the all-absent case to a page when HA is UP.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=0.0)
    ok, msg = check.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_nut_server_down_defers_not_double_pages(monkeypatch):
    # A real NUT-server outage (peanut down / USB unplugged): HA drops the numeric charge+runtime
    # sensors (unavailable) while the replace-battery template FLOORS to 0 (stays present) ->
    # charge=None, runtime=None, replace=0.0. That's the nut pod liveness probe's page, NOT an
    # entity rename, so check_ups must DEFER (up) — not partial-absence page with a misdirecting
    # "entity renamed?" msg (the 2026-07-14 review M1 double-page bug).
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, None, None, replace=0.0)
    ok, msg = check.check_ups()
    assert ok and "NUT numeric arms" in msg
    assert check._ups_down_streak == 0


def test_check_ups_replace_battery_pages(monkeypatch):
    # RB verdict from the self-test -> down after the streak even with a full charge / good runtime.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900, replace=1.0)
    ok1, _ = check.check_ups()
    assert ok1  # streak grace on the first cycle
    ok2, msg2 = check.check_ups()
    assert not ok2 and "replace-battery" in msg2


def test_check_ups_partial_absence_pages_not_silently_survives(monkeypatch):
    # charge+runtime present but the replace arm vanished (entity rename) -> flag, don't monitor the
    # survivor silently. Goes through the streak (HA-restart grace) then pages, naming the missing arm.
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 900, replace=None)
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = check.check_ups()
    assert not ok2 and "absent" in msg2 and "replace-battery" in msg2


def test_check_ups_single_low_runtime_is_suppressed_then_pages(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 60)  # runtime 1m < 5m floor
    ok1, msg1 = check.check_ups()
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE default 2
    ok2, msg2 = check.check_ups()
    assert not ok2 and "runtime" in msg2


def test_check_ups_recovery_resets_streak(monkeypatch):
    check._ups_down_streak = 0
    _ups_scalars(monkeypatch, 100, 60)
    check.check_ups()  # streak advances to 1
    _ups_scalars(monkeypatch, 100, 900)  # healthy again
    ok, _ = check.check_ups()
    assert ok
    assert check._ups_down_streak == 0


def test_check_ups_disabled_when_no_queries(monkeypatch):
    check._ups_down_streak = 0
    monkeypatch.setattr(check, "UPS_CHARGE_QUERY", "")
    monkeypatch.setattr(check, "UPS_RUNTIME_QUERY", "")
    monkeypatch.setattr(check, "UPS_REPLACE_QUERY", "")
    ok, msg = check.check_ups()
    assert ok and "disabled" in msg


# ── pi_pressure (Pi load / memory / disk headroom via the Pi's glances API) ──


MB = 1048576

LOAD_OK = {"min5": 0.8, "cpucore": 4}
MEM_OK = {"available": 150 * MB}
# Glances in its container sees its own bind-mounts (/etc/resolv.conf etc.), all backed
# by the SD card device with the HOST fs usage percent — so entries are keyed by
# device_name, and one device appears many times.
FS_OK = [
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/resolv.conf", "percent": 3.3},
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 3.3},
]


def test_pi_pressure_ok():
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, FS_OK, 1.5, 50, 90)
    assert ok
    assert "0.20/core" in msg and "150MB" in msg and "disk 3%" in msg


def test_pi_pressure_high_load_alerts():
    # 2026-06-11 fwupd incident signature: load5 ~7.2 on 4 cores while every
    # container healthcheck timed out (mem available still ~150MB at that instant)
    ok, msg = check.pi_pressure({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_pressure_low_mem_alerts():
    ok, msg = check.pi_pressure(
        {"min5": 0.4, "cpucore": 4}, {"available": 13 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "13MB" in msg


def test_pi_pressure_full_disk_alerts_naming_device():
    fs = [
        {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 94.0}
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert "/dev/mmcblk0p2" in msg and "94" in msg


def test_pi_pressure_duplicate_device_entries_alert_once():
    fs = [
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/resolv.conf",
            "percent": 94.0,
        },
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/hostname",
            "percent": 94.0,
        },
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert msg.count("/dev/mmcblk0p2") == 1


def test_pi_pressure_both_breaches_named():
    ok, msg = check.pi_pressure(
        {"min5": 8.0, "cpucore": 4}, {"available": 10 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "load5" in msg and "available" in msg


def test_pi_pressure_at_threshold_is_ok():
    # strictly greater / strictly less, like the other checks' threshold semantics
    fs = [{"device_name": "/dev/mmcblk0p2", "mnt_point": "/", "percent": 90.0}]
    ok, _ = check.pi_pressure(
        {"min5": 6.0, "cpucore": 4}, {"available": 50 * MB}, fs, 1.5, 50, 90
    )
    assert ok


def test_pi_pressure_missing_fields_alert():
    ok, msg = check.pi_pressure({}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_pressure_empty_fs_alerts():
    # a glances fs-plugin regression must surface, not silently pass (same principle
    # as the load/mem missing-field handling)
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, [], 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_pressure_zero_cores_alerts_not_divides():
    ok, msg = check.pi_pressure({"min5": 1.0, "cpucore": 0}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_check_disabled_without_url():
    # PI_GLANCES_URL defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_pi_pressure()
    assert ok
    assert "disabled" in msg.lower()


def test_pi_check_down_on_pressure(monkeypatch):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", _seq({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert "load5" in msg


def test_pi_check_up_when_quiet(monkeypatch):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", _seq({"min5": 0.4, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, _ = check.check_pi_pressure()
    assert ok


# check_longhorn_volumes — replica redundancy on the storage layer


def _longhorn_series(pvc, state, pod="longhorn-manager-a"):
    return ({"pvc": pvc, "state": state, "pod": pod, "volume": "pvc-" + pvc}, 1.0)


def _arm_longhorn(monkeypatch, vector, volumes=43.0, consecutive=3):
    monkeypatch.setattr(check, "_longhorn_degraded_streak", 0)
    monkeypatch.setattr(check, "LONGHORN_CONSECUTIVE", consecutive)
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: volumes)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vector)


def test_longhorn_all_redundant_is_up_and_reports_the_volume_count(monkeypatch):
    _arm_longhorn(monkeypatch, [])
    ok, msg = check.check_longhorn_volumes()
    assert ok
    assert "43 volume(s) redundant" in msg


def test_longhorn_degraded_holds_up_until_the_threshold_then_pages(monkeypatch):
    _arm_longhorn(monkeypatch, [_longhorn_series("freshrss-config", "degraded")])
    # A node drain degrades every volume on the departing node by design, so the first
    # cycles must hold `up` — otherwise this monitor pages every Sunday reboot.
    ok1, msg1 = check.check_longhorn_volumes()
    ok2, _ = check.check_longhorn_volumes()
    ok3, msg3 = check.check_longhorn_volumes()
    assert ok1 and ok2
    assert "1/3" in msg1
    assert not ok3
    assert "freshrss-config" in msg3
    assert "single-copy" in msg3


def test_longhorn_recovery_resets_the_streak(monkeypatch):
    _arm_longhorn(monkeypatch, [_longhorn_series("freshrss-config", "degraded")])
    check.check_longhorn_volumes()
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    assert check.check_longhorn_volumes()[0]
    assert check._longhorn_degraded_streak == 0


def test_longhorn_absent_metric_is_not_green(monkeypatch):
    # The whole point of the arm: an empty degraded-selector looks identical whether the
    # cluster is healthy or the longhorn scrape job is dead. The volume count is the input
    # assertion, so a missing family must fail closed rather than read as "none degraded".
    _arm_longhorn(monkeypatch, [], volumes=None)
    ok1, msg1 = check.check_longhorn_volumes()
    assert ok1  # first cycle rides the grace, but says why
    assert "UNMONITORED" in msg1
    check.check_longhorn_volumes()
    ok3, msg3 = check.check_longhorn_volumes()
    assert not ok3
    assert "not the same as healthy" in msg3


def test_longhorn_dedupes_a_volume_reported_by_both_managers(monkeypatch):
    # The two longhorn-manager pods report disjoint subsets today, but a volume moving
    # between them must not be double-counted into the message.
    _arm_longhorn(
        monkeypatch,
        [
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-a"),
            _longhorn_series("karakeep-data", "degraded", pod="longhorn-manager-b"),
        ],
        consecutive=1,
    )
    ok, msg = check.check_longhorn_volumes()
    assert not ok
    assert "1 degraded" in msg


def test_longhorn_faulted_outranks_degraded_for_the_same_volume(monkeypatch):
    _arm_longhorn(
        monkeypatch,
        [
            _longhorn_series("valheim-data", "degraded"),
            _longhorn_series("valheim-data", "faulted"),
        ],
        consecutive=1,
    )
    ok, msg = check.check_longhorn_volumes()
    assert not ok
    assert "1 faulted" in msg
    assert "degraded" not in msg


def test_longhorn_selects_on_the_state_label_not_a_value_ordinal():
    # longhorn_volume_robustness is ONE-HOT over `state` with value 0/1. An earlier proposal
    # for this arm compared the value to 2 ("degraded"), which no series ever equals. Pin the
    # label-based selector so that mistake cannot come back.
    queries = []

    def record(promql, *a, **k):
        queries.append(promql)
        return []

    saved_vector, saved_scalar = check.prom_vector, check.prom_scalar
    try:
        check.prom_vector = record
        check.prom_scalar = lambda *a, **k: 43.0
        check.check_longhorn_volumes()
    finally:
        check.prom_vector, check.prom_scalar = saved_vector, saved_scalar
    assert len(queries) == 1
    assert 'state=~"degraded|faulted"' in queries[0]
    assert "== 2" not in queries[0]


#
# dri-device-plugin has no probe, and a container without a readinessProbe is Ready the instant it
# starts. So a plugin that wedges internally keeps a Running, Ready, fully-available DaemonSet
# while kubelet deregisters the extended resource - the DaemonSet arm is structurally blind to it,
# and the only other evidence (jellyfin and tdarr unschedulable) does not appear until they next
# reschedule. The repo recorded this omission as "covered by monitor-bridge's check", which was a
# true sentence about a check that reads a different metric.


def test_the_query_uses_the_label_kube_state_metrics_actually_emits():
    """KSM sanitizes the resource name into the label, so the configured name never matches.

    Live on 2026-08-20: both nodes advertised `devic.es/dri: 4`, KSM emitted
    `resource="devic_es_dri"`, and the query for the unsanitised name matched nothing - which this
    check reads as the plugin having deregistered. The monitor went DOWN on a healthy cluster and
    stayed there until the sanitiser landed. The operator-facing name stays the one
    `kubectl describe node` prints; only the query is sanitised.
    """
    assert check.ksm_resource_label("devic.es/dri") == "devic_es_dri"
    assert check.ksm_resource_label("nvidia.com/gpu") == "nvidia_com_gpu"
    assert check.ksm_resource_label("cpu") == "cpu"


def test_missing_extended_resource_names_both_the_resource_and_its_label():
    """A false fault and a real one look identical unless the alert names the label it queried."""
    ok, msg = check.extended_resource_verdict(["devic.es/dri"], {"devic.es/dri": 0}, 12)
    assert ok is False
    assert "devic.es/dri" in msg
    assert "devic_es_dri" in msg


def test_resource_absent_from_the_map_is_a_fault():
    """An absent key and a zero count mean the same thing: nothing advertises it."""
    ok, _ = check.extended_resource_verdict(["devic.es/dri"], {}, 12)
    assert ok is False


def test_advertised_resource_passes_and_reports_node_count():
    ok, msg = check.extended_resource_verdict(["devic.es/dri"], {"devic.es/dri": 1}, 12)
    assert ok is True
    assert "1 node(s)" in msg


def test_no_series_at_all_is_inert_not_green_and_not_red():
    """The collector not running must not read as health or as fault - it must say so.

    Passing silently would be exactly the "check that cannot read its input answers anyway"
    failure this arm exists to fix. Failing would page for a kube-state-metrics config change
    nobody made. Naming it is the only honest option.
    """
    ok, msg = check.extended_resource_verdict(["devic.es/dri"], {}, 0)
    assert ok is True
    assert "INERT" in msg
    assert "devic.es/dri" in msg


def test_the_inert_arm_takes_prom_scalars_real_empty_value():
    """prom_scalar returns None on an empty query, never 0 - so None is what production feeds here.

    Testing only 0 would leave the real call path unexercised: both are falsy today, but the
    fixture would stop matching the producer the moment that branch grew anything sharper than a
    truthiness test.
    """
    ok, msg = check.extended_resource_verdict(["devic.es/dri"], {}, None)
    assert ok is True
    assert "INERT" in msg


def test_several_resources_are_all_checked():
    ok, msg = check.extended_resource_verdict(
        ["devic.es/dri", "example.com/fpga"],
        {"devic.es/dri": 2, "example.com/fpga": 0},
        12,
    )
    assert ok is False
    assert "example.com/fpga" in msg


def test_nothing_expected_is_trivially_ok():
    ok, _ = check.extended_resource_verdict([], {}, 12)
    assert ok is True
