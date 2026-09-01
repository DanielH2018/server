"""The UPS: battery health via Home Assistant's Prometheus-scraped sensors.

The absence arms are the substance. A missing series means the NUT server or the HA scrape is
down, not that the battery is fine, so the check defers to the scrape-target monitor rather than
paging twice — except when HA is scraping and the series is still absent, which is a real fault.
"""

import pytest

import bridge_config
import bridge_streaks
import bridge_io
import checks_host


@pytest.mark.parametrize(
    ("charge", "runtime", "replace", "ok", "must_contain", "must_not_contain"),
    [
        pytest.param(
            100,
            900,
            0,
            True,
            ("battery 100%", "runtime 15.0m", "self-test ok"),
            (),
            id="ok",
        ),
        pytest.param(
            30, 900, 0, False, ("battery 30%",), ("runtime",), id="low_charge_is_named"
        ),
        pytest.param(
            100,
            120,
            0,
            False,
            ("runtime 2.0m",),
            ("battery",),
            id="low_runtime_is_named",
        ),
        pytest.param(
            20,
            60,
            0,
            False,
            ("battery 20%", "runtime 1.0m"),
            (),
            id="both_breaches_named",
        ),
        pytest.param(
            100,
            900,
            1,
            False,
            ("replace-battery",),
            (),
            # The UPS's own RB self-test verdict trips even while charge/runtime read fine —
            # earliest signal.
            id="replace_battery_pages_even_with_good_runway",
        ),
        # strict `<`, so exactly at the floor is fine
        pytest.param(50, 300, 0, True, (), (), id="at_threshold_is_ok"),
        pytest.param(
            None,
            120,
            None,
            False,
            ("runtime",),
            ("battery",),
            # only runtime present and low -> pages on runtime alone; the other arms are ignored
            id="absent_arm_is_skipped",
        ),
    ],
)
def test_ups_health(charge, runtime, replace, ok, must_contain, must_not_contain):
    result_ok, msg = checks_host.ups_health(charge, runtime, replace, 50, 300)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg
    for s in must_not_contain:
        assert s not in msg


def _ups_scalars(monkeypatch, charge, runtime, replace=0.0, ha_up=None):
    def fake(q):
        if q == bridge_config.UPS_CHARGE_QUERY:
            return charge
        if q == bridge_config.UPS_RUNTIME_QUERY:
            return runtime
        if q == bridge_config.UPS_REPLACE_QUERY:
            return replace
        if q == bridge_config.UPS_HA_UP_QUERY:
            return ha_up
        return None

    monkeypatch.setattr(bridge_io, "prom_scalar", fake)


def test_check_ups_healthy_is_up(monkeypatch):
    _ups_scalars(monkeypatch, 100, 900)
    ok, msg = checks_host.check_ups()
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(monkeypatch):
    # HA scrape down (ha_up None via the fake) -> all arms absent defers to Scrape Targets.
    _ups_scalars(monkeypatch, None, None, replace=None)
    ok, msg = checks_host.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_all_absent_but_ha_scraping_pages(monkeypatch):
    # Every UPS entity renamed/removed at once while HA keeps scraping (up{home-assistant}==1):
    # Scrape Targets can't see it, so the old all-absent defer silently unmonitored the UPS. Now it
    # pages through the streak (naming the missing arms) instead of deferring.
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=1.0)
    ok1, msg1 = checks_host.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks_host.check_ups()
    assert not ok2 and "absent" in msg2
    assert bridge_streaks._down_streaks.get("ups", 0) == 2


def test_check_ups_all_absent_ha_down_still_defers(monkeypatch):
    # HA scrape affirmatively down (up==0) with all arms absent -> still defer (Scrape Targets owns
    # the HA-source outage); the up-gate only flips the all-absent case to a page when HA is UP.
    _ups_scalars(monkeypatch, None, None, replace=None, ha_up=0.0)
    ok, msg = checks_host.check_ups()
    assert ok and "no UPS data" in msg


def test_check_ups_nut_server_down_defers_not_double_pages(monkeypatch):
    # A real NUT-server outage (peanut down / USB unplugged): HA drops the numeric charge+runtime
    # sensors (unavailable) while the replace-battery template FLOORS to 0 (stays present) ->
    # charge=None, runtime=None, replace=0.0. That's the nut pod liveness probe's page, NOT an
    # entity rename, so check_ups must DEFER (up) — not partial-absence page with a misdirecting
    # "entity renamed?" msg (the 2026-07-14 review M1 double-page bug).
    _ups_scalars(monkeypatch, None, None, replace=0.0)
    ok, msg = checks_host.check_ups()
    assert ok and "NUT numeric arms" in msg
    assert bridge_streaks._down_streaks.get("ups", 0) == 0


def test_check_ups_replace_battery_pages(monkeypatch):
    # RB verdict from the self-test -> down after the streak even with a full charge / good runtime.
    _ups_scalars(monkeypatch, 100, 900, replace=1.0)
    ok1, _ = checks_host.check_ups()
    assert ok1  # streak grace on the first cycle
    ok2, msg2 = checks_host.check_ups()
    assert not ok2 and "replace-battery" in msg2


def test_check_ups_partial_absence_pages_not_silently_survives(monkeypatch):
    # charge+runtime present but the replace arm vanished (entity rename) -> flag, don't monitor the
    # survivor silently. Goes through the streak (HA-restart grace) then pages, naming the missing arm.
    _ups_scalars(monkeypatch, 100, 900, replace=None)
    ok1, msg1 = checks_host.check_ups()
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks_host.check_ups()
    assert not ok2 and "absent" in msg2 and "replace-battery" in msg2


def test_check_ups_single_low_runtime_is_suppressed_then_pages(monkeypatch):
    _ups_scalars(monkeypatch, 100, 60)  # runtime 1m < 5m floor
    ok1, msg1 = checks_host.check_ups()
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE default 2
    ok2, msg2 = checks_host.check_ups()
    assert not ok2 and "runtime" in msg2


def test_check_ups_recovery_resets_streak(monkeypatch):
    _ups_scalars(monkeypatch, 100, 60)
    checks_host.check_ups()  # streak advances to 1
    _ups_scalars(monkeypatch, 100, 900)  # healthy again
    ok, _ = checks_host.check_ups()
    assert ok
    assert bridge_streaks._down_streaks.get("ups", 0) == 0


def test_check_ups_disabled_when_no_queries(monkeypatch):
    monkeypatch.setattr(bridge_config, "UPS_CHARGE_QUERY", "")
    monkeypatch.setattr(bridge_config, "UPS_RUNTIME_QUERY", "")
    monkeypatch.setattr(bridge_config, "UPS_REPLACE_QUERY", "")
    ok, msg = checks_host.check_ups()
    assert ok and "disabled" in msg


# ── pi_pressure (Pi load / memory / disk headroom via the Pi's glances API) ──
