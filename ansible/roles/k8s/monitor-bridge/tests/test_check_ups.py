"""The UPS: battery health from nut-exporter, with Home Assistant as the fallback.

The absence arms are the substance. A missing series means a source is down, not that the
battery is fine, so the check defers to the scrape-target monitor rather than paging twice —
except when a source IS scraping and the series is still absent, which is a real fault.

Which source answers is decided in PromQL (`max(A) or max(B)`), not here, so these tests drive
each arm by its configured query string and stay indifferent to it. What they do pin is that
the mains-loss arm exists, outranks the runway arms, and holds its own streak.
"""

from dataclasses import replace

import pytest

import bridge.config
import bridge.streaks
import bridge.net
import checks.host_thermal
import verdicts.host_power


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
    result_ok, msg = checks.host_thermal.ups_health(charge, runtime, replace, 50, 300)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg
    for s in must_not_contain:
        assert s not in msg


def _ups_scalars(
    cfg, monkeypatch, charge, runtime, replace=0.0, source_up=None, on_battery=0.0
):
    def fake(_cfg, q):
        if q == cfg.UPS_CHARGE_QUERY:
            return charge
        if q == cfg.UPS_RUNTIME_QUERY:
            return runtime
        if q == cfg.UPS_REPLACE_QUERY:
            return replace
        if q == cfg.UPS_ON_BATTERY_QUERY:
            return on_battery
        if q == cfg.UPS_SOURCE_UP_QUERY:
            return source_up
        return None

    monkeypatch.setattr(bridge.net, "prom_scalar", fake)


def test_check_ups_healthy_is_up(monkeypatch, cfg):
    _ups_scalars(cfg, monkeypatch, 100, 900)
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(monkeypatch, cfg):
    # HA scrape down (ha_up None via the fake) -> all arms absent defers to Scrape Targets.
    # on_battery=None with the rest: the on-battery arm is in the same census since #1630, so
    # "all arms absent" now means all FOUR.
    _ups_scalars(cfg, monkeypatch, None, None, replace=None, on_battery=None)
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "no UPS data" in msg


def test_check_ups_all_absent_but_ha_scraping_pages(monkeypatch, cfg):
    # Every UPS entity renamed/removed at once while HA keeps scraping (up{home-assistant}==1):
    # Scrape Targets can't see it, so the old all-absent defer silently unmonitored the UPS. Now it
    # pages through the streak (naming the missing arms) instead of deferring.
    _ups_scalars(
        cfg, monkeypatch, None, None, replace=None, source_up=1.0, on_battery=None
    )
    ok1, msg1 = checks.host_thermal.check_ups(cfg)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "absent" in msg2
    assert bridge.streaks._down_streaks.get("ups", 0) == 2


def test_check_ups_all_absent_ha_down_still_defers(monkeypatch, cfg):
    # HA scrape affirmatively down (up==0) with all arms absent -> still defer (Scrape Targets owns
    # the HA-source outage); the up-gate only flips the all-absent case to a page when HA is UP.
    _ups_scalars(
        cfg, monkeypatch, None, None, replace=None, source_up=0.0, on_battery=None
    )
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "no UPS data" in msg


def test_check_ups_nut_server_down_defers_not_double_pages(monkeypatch, cfg):
    # A real NUT-server outage (peanut down / USB unplugged): HA drops the numeric charge+runtime
    # sensors (unavailable) while the replace-battery template FLOORS to 0 (stays present) ->
    # charge=None, runtime=None, replace=0.0. That's the nut pod liveness probe's page, NOT an
    # entity rename, so check_ups must DEFER (up) — not partial-absence page with a misdirecting
    # "entity renamed?" msg (the 2026-07-14 review M1 double-page bug).
    _ups_scalars(cfg, monkeypatch, None, None, replace=0.0)
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "NUT numeric arms" in msg
    assert bridge.streaks._down_streaks.get("ups", 0) == 0


def test_check_ups_replace_battery_pages(monkeypatch, cfg):
    # RB verdict from the self-test -> down after the streak even with a full charge / good runtime.
    _ups_scalars(cfg, monkeypatch, 100, 900, replace=1.0)
    ok1, _ = checks.host_thermal.check_ups(cfg)
    assert ok1  # streak grace on the first cycle
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "replace-battery" in msg2


def test_check_ups_partial_absence_pages_not_silently_survives(monkeypatch, cfg):
    # charge+runtime present but the replace arm vanished (entity rename) -> flag, don't monitor the
    # survivor silently. Goes through the streak (HA-restart grace) then pages, naming the missing arm.
    _ups_scalars(cfg, monkeypatch, 100, 900, replace=None)
    ok1, msg1 = checks.host_thermal.check_ups(cfg)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "absent" in msg2 and "replace-battery" in msg2


def test_check_ups_single_low_runtime_is_suppressed_then_pages(monkeypatch, cfg):
    _ups_scalars(cfg, monkeypatch, 100, 60)  # runtime 1m < 5m floor
    ok1, msg1 = checks.host_thermal.check_ups(cfg)
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE default 2
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "runtime" in msg2


def test_check_ups_recovery_resets_streak(monkeypatch, cfg):
    _ups_scalars(cfg, monkeypatch, 100, 60)
    checks.host_thermal.check_ups(cfg)  # streak advances to 1
    _ups_scalars(cfg, monkeypatch, 100, 900)  # healthy again
    ok, _ = checks.host_thermal.check_ups(cfg)
    assert ok
    assert bridge.streaks._down_streaks.get("ups", 0) == 0


def test_check_ups_disabled_when_no_queries(monkeypatch, cfg):
    cfg = replace(
        cfg,
        UPS_CHARGE_QUERY="",
        UPS_RUNTIME_QUERY="",
        UPS_REPLACE_QUERY="",
        UPS_ON_BATTERY_QUERY="",
    )
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "disabled" in msg


# ── mains loss: the arm the direct NUT series made possible (issue #1548) ──
#
# The reason the UPS alert path moved off Home Assistant. HA re-exports the same UPS, so with HA
# primary the alert path went down with the workload the UPS most obviously protects. These are
# accept/reject pairs like the thermal arms': one input the arm must flag, one it must not.


def test_the_on_battery_arm_flags_an_asserted_ob_flag():
    verdict = verdicts.host_power.ups_on_battery_verdict(1.0)
    assert verdict is not None
    assert not verdict[0]
    assert "on battery" in verdict[1]


def test_the_on_battery_arm_says_nothing_while_mains_power_is_present():
    assert verdicts.host_power.ups_on_battery_verdict(0.0) is None


def test_the_on_battery_arm_says_nothing_when_the_series_is_absent():
    """Absence is the exporter going quiet, which the all-arms-absent branch and the nut pod's
    liveness probe own between them. Flagging here would double-page a source outage."""
    assert verdicts.host_power.ups_on_battery_verdict(None) is None


def test_check_ups_pages_on_sustained_mains_loss(monkeypatch, cfg):
    """The Verify-by for #1548: the check goes red off network_ups_tools_ups_status{flag="OB"},
    with the runway arms reading perfectly healthy throughout — which is what they do for most
    of a real outage."""
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=1.0)
    ok1, msg1 = checks.host_thermal.check_ups(cfg)
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE grace rides out a brownout
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "on battery" in msg2


def test_mains_loss_outranks_a_healthy_runway_message(monkeypatch, cfg):
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=1.0)
    _, msg = checks.host_thermal.check_ups(cfg)
    assert "battery 100%" not in msg, "the outage is the report, not the runway"


def test_the_on_battery_arm_holds_its_own_streak(monkeypatch, cfg):
    """Separate keys, for the reason check_host_temp's arms record: a brownout cycle and a
    low-runway cycle sharing one counter would page at half the intended grace."""
    _ups_scalars(cfg, monkeypatch, 100, 60, on_battery=1.0)
    checks.host_thermal.check_ups(cfg)
    assert bridge.streaks._down_streaks.get("ups_on_battery", 0) == 1
    assert bridge.streaks._down_streaks.get("ups", 0) == 0


def test_restored_mains_clears_the_on_battery_streak(monkeypatch, cfg):
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=1.0)
    checks.host_thermal.check_ups(cfg)
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=0.0)
    ok, _ = checks.host_thermal.check_ups(cfg)
    assert ok
    assert bridge.streaks._down_streaks.get("ups_on_battery", 0) == 0


def test_the_on_battery_series_going_missing_alone_pages(monkeypatch, cfg):
    """The absence half of the arm: a rename of the OB series alone must not go quiet (#1630).

    The three runway arms report normally, so nothing else in the check has anything to say and
    the monitor would read green while mains-loss monitoring was off — the inert-check shape.
    The arm is in `configured`, so its absence is a partial absence and pages through the same
    UPS_CONSECUTIVE streak as a charge or runtime rename.
    """
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=None)
    ok1, msg1 = checks.host_thermal.check_ups(cfg)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg)
    assert not ok2 and "on-battery" in msg2 and "absent" in msg2


def test_the_on_battery_series_present_is_not_flagged_absent(monkeypatch, cfg):
    """The accepting half: a reporting OB series leaves the up message as it was."""
    _ups_scalars(cfg, monkeypatch, 100, 900, on_battery=0.0)
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "absent" not in msg


def test_a_nut_outage_still_defers_rather_than_naming_the_on_battery_rename(
    monkeypatch, cfg
):
    """OB is absent in a NUT outage too, and that must not read as a rename.

    The numeric-arms-absent defer is judged before the partial-absence page, so an exporter
    death reaches the nut liveness probe's message rather than "entity renamed?" — adding OB to
    the census must not reorder that.
    """
    _ups_scalars(cfg, monkeypatch, None, None, replace=0.0, on_battery=None)
    ok, msg = checks.host_thermal.check_ups(cfg)
    assert ok and "NUT server/integration down" in msg


def test_the_on_battery_arm_is_off_when_no_query_is_configured(monkeypatch, cfg):
    off = replace(cfg, UPS_ON_BATTERY_QUERY="")
    _ups_scalars(off, monkeypatch, 100, 900, on_battery=1.0)
    ok, msg = checks.host_thermal.check_ups(off)
    assert ok and "battery 100%" in msg


# ── pi_pressure (Pi load / memory / disk headroom via the Pi's glances API) ──
