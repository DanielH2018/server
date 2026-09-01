"""Home Assistant: the automation-engine heartbeat and the ip_ban arm.

The heartbeat reads an input_datetime a 1-minute automation stamps, with hysteresis to ride out
the ~120s deploy restart. The ip_ban arm is separate and skips that grace: on 2026-08-23 a
banned infra IP 403'd the probes into a crash loop, which the heartbeat alone could not see.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import bridge_config
import check

_REPO = Path(__file__).resolve().parents[5]

# ── HA automation-engine heartbeat (input_datetime stamped by a 1-min automation) ──
# ha_heartbeat_fresh reads last_changed off the /api/states/input_datetime.ha_heartbeat
# payload: fresh => the scheduler ran recently; stale/missing => wedged or never ran.
HB_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ha_state(last_changed, state="2026-06-06 11:59:00"):
    """Minimal HA state shape — only last_changed is read by the check."""
    return {
        "entity_id": "input_datetime.ha_heartbeat",
        "state": state,
        "last_changed": last_changed,
        "last_updated": last_changed,
    }


@pytest.mark.parametrize(
    ("state", "ok", "must_contain"),
    [
        pytest.param(
            _ha_state("2026-06-06T11:59:00Z"), True, ("fresh",), id="fresh_is_ok"
        ),  # 60s old
        pytest.param(
            _ha_state("2026-06-06T11:50:00Z"), False, ("stale",), id="stale_is_down"
        ),  # 600s old
        pytest.param(
            _ha_state("2026-06-06T11:55:00Z"), True, (), id="at_threshold_is_ok"
        ),  # exactly 300s
        pytest.param(
            {"state": "unknown"}, False, (), id="missing_last_changed_is_down"
        ),
        pytest.param(None, False, (), id="none_state_is_down"),
    ],
)
def test_ha_heartbeat_fresh(state, ok, must_contain):
    result_ok, msg = check.ha_heartbeat_fresh(state, 300, now=HB_NOW)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


# ── check_ha_heartbeat hysteresis (rides out the ~120s deploy/restart) ──────
# A redeploy makes the HTTP API briefly unreachable AND leaves the automation
# scheduler a beat behind, so a single cycle can read unreachable OR stale. Like
# CPU_CONSECUTIVE, only HA_CONSECUTIVE straight down-cycles page; a single blip
# pushes up with a streak msg. ha_heartbeat_fresh uses the real clock (no `now`
# override on this path), so payloads are built relative to real now.
def _ha_payload(age_s):
    lc = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return _ha_state(lc)


def _ha_cycle(monkeypatch, age_s=600, raises=False, banned=0):
    monkeypatch.setattr(bridge_config, "HA_URL", "http://home-assistant:8123")
    monkeypatch.setattr(bridge_config, "HA_TOKEN", "tok")
    # The ip_ban arm queries Loki via loki_count. Patch it explicitly rather than letting it fall
    # through the _get_json stub below: that stub returns an HA state payload, so the arm would
    # take its fail-open path for an accidental reason and stop testing the hysteresis cleanly.
    monkeypatch.setattr(check, "loki_count", lambda *a, **k: banned)
    if raises:

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(check, "_get_json", boom)
    else:
        monkeypatch.setattr(check, "_get_json", lambda *a, **k: _ha_payload(age_s))
    return check.check_ha_heartbeat()


def test_ha_heartbeat_single_stale_cycle_is_suppressed(monkeypatch):
    # One stale cycle (a deploy mid-recreate) must NOT page — pushes up with a streak msg.
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    assert "1/2" in msg  # streak progress vs default HA_CONSECUTIVE=2


def test_ha_heartbeat_two_consecutive_stale_cycles_alert(monkeypatch):
    # Default HA_CONSECUTIVE=2: the 2nd straight stale cycle is a genuinely wedged HA -> down.
    ok, _ = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert not ok
    assert "stale" in msg


def test_ha_heartbeat_fresh_read_resets_streak(monkeypatch):
    # stale, then fresh -> never down (a recovered deploy clears the streak).
    assert _ha_cycle(monkeypatch, age_s=600)[0]
    ok, msg = _ha_cycle(monkeypatch, age_s=60)  # scheduler resumed, heartbeat fresh
    assert ok
    assert "fresh" in msg
    # the next stale cycle starts a NEW streak, so it's suppressed again
    ok, msg = _ha_cycle(monkeypatch, age_s=600)
    assert ok
    assert "1/2" in msg


def test_ha_heartbeat_unreachable_api_rides_grace(monkeypatch):
    # The recreate-window connection error must ride the SAME grace, not page immediately.
    ok, msg = _ha_cycle(monkeypatch, raises=True)
    assert ok
    assert "1/2" in msg


def test_ha_ban_no_events_is_ok():
    ok, msg = check.ha_ban_verdict(0, "1h")
    assert ok
    assert "no ip_ban events" in msg


def test_ha_ban_none_series_is_ok():
    # None and 0 are the same healthy answer: HA logs nothing when it bans nobody, so an empty
    # vector is what a healthy cluster looks like — unlike loki_ingestion_fresh, where silence
    # IS the fault.
    ok, _ = check.ha_ban_verdict(None, "1h")
    assert ok


def test_ha_ban_event_is_down():
    ok, msg = check.ha_ban_verdict(1, "1h")
    assert not ok
    assert "ip_ban fired 1 time(s)" in msg
    assert "ip_bans.yaml" in msg


def test_ha_ban_wins_the_message_over_a_healthy_heartbeat(monkeypatch):
    # A ban pages even while the heartbeat itself is fresh — the two arms are independent, and
    # the ban text leads because it names the actionable fault.
    ok, msg = _ha_cycle(monkeypatch, age_s=60, banned=3)
    assert not ok
    assert msg.startswith("HA ip_ban fired 3 time(s)")
    assert "fresh" in msg  # the heartbeat's own verdict is preserved, not dropped


def test_ha_ban_skips_the_deploy_grace(monkeypatch):
    # down_streak exists for transients. A ban persists in /config/ip_bans.yaml until a human
    # clears it, so it must page on the FIRST cycle rather than ride the 2-cycle grace.
    ok, _ = _ha_cycle(monkeypatch, age_s=60, banned=1)
    assert not ok


def test_ha_ban_arm_fails_open_when_loki_errors(monkeypatch):
    # A Loki outage must not page the HA monitor. ha_heartbeat is deliberately NOT in
    # LOKI_DEPENDENT (that would suppress the whole check and blind the real heartbeat), so the
    # arm swallows the error and keeps the heartbeat's verdict.

    def boom(*a, **k):
        raise OSError("loki unreachable")

    monkeypatch.setattr(check, "loki_count", boom)
    monkeypatch.setattr(bridge_config, "HA_URL", "http://home-assistant:8123")
    monkeypatch.setattr(bridge_config, "HA_TOKEN", "tok")
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _ha_payload(60))
    ok, msg = check.check_ha_heartbeat()
    assert ok
    assert "ip_ban arm unavailable" in msg


def test_ha_heartbeat_disabled_when_no_url_token(monkeypatch):
    monkeypatch.setattr(bridge_config, "HA_URL", "")
    monkeypatch.setattr(bridge_config, "HA_TOKEN", "")
    ok, msg = check.check_ha_heartbeat()
    assert ok
    assert "disabled" in msg


# Loki's Kuma /ready probe stays green even if promtail stops shipping (DOCKER_HOST
# break, positions-file corruption, label regression) — a silently-dead log pipeline.
# This check counts ingested log lines for an always-active stream over a window and
# goes down when zero: a freshness watchdog analogous to the SMART/restore-drill ones.
