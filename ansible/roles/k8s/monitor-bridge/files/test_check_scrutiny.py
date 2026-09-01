"""Scrutiny: SMART freshness, SMART health, and NVMe wear.

Three arms that fail differently. Freshness catches a collector that stopped running; health
reads device_status, which freshness alone cannot see; and wear reads percentage_used, which
ships with thresh=100 so device_status will never warn on it.
"""

from datetime import datetime, timezone


import bridge_config
import bridge_io
import check


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

    monkeypatch.setattr(bridge_io, "_get_json", fake_get_json)
    monkeypatch.setattr(bridge_config, "SCRUTINY_URL", "http://scrutiny:8080")
    summary = {
        "w1": {"device": {"device_name": "nvme0", "model_name": "SHPP41-500GM"}},
        "w2": {"device": {"device_name": "sda", "archived": True}},
    }
    devices = check.scrutiny_wear_devices(summary)
    assert devices == [("nvme0 (SHPP41-500GM)", 7)]
    assert fetched == ["http://scrutiny:8080/api/device/w1/details"]


# ── ups (battery health via HA's Prometheus-scraped UPS sensors) ─────────────
