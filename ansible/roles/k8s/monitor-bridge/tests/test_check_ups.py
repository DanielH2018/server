"""The UPS: battery health from nut-exporter, the one source every arm reads.

The absence arms are the substance. A missing series means the nut scrape is down, not that the
battery is fine, so the check defers to the scrape-target monitor rather than paging twice —
except when the scrape IS answering and the series is still absent, which is a real fault.

These tests drive each arm
by its configured query string and stay indifferent to what that string is. What they do pin is
that the mains-loss arm exists, outranks the runway arms, and holds its own streak.
"""

from dataclasses import replace

import pytest

import checks.host_thermal
import verdicts.host_power
from _fake_sources import FakeSources


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
    cfg, charge, runtime, replace=0.0, source_up=None, on_battery=0.0
) -> FakeSources:
    def fake(q):
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

    return FakeSources(prom_scalar=fake)


def test_check_ups_healthy_is_up(cfg):
    src = _ups_scalars(cfg, 100, 900)
    ok, msg = checks.host_thermal.check_ups(cfg, src)
    assert ok and "battery 100%" in msg and "self-test ok" in msg


def test_check_ups_absent_data_defers_to_scrape_targets(cfg):
    # Unqueryable up-gate (source_up None via the fake) -> all arms absent defers to Scrape
    # Targets. on_battery=None with the rest: the on-battery arm is in the same census, so
    # "all arms absent" means all FOUR.
    src = _ups_scalars(cfg, None, None, replace=None, on_battery=None)
    ok, msg = checks.host_thermal.check_ups(cfg, src)
    assert ok and "no UPS data" in msg


def test_check_ups_all_absent_but_nut_scraping_pages(cfg):
    # Every UPS series renamed/removed at once while the nut job keeps scraping (up==1): Scrape
    # Targets can't see it, so the old all-absent defer silently unmonitored the UPS. It pages
    # through the streak (naming the missing arms) instead of deferring.
    src = _ups_scalars(cfg, None, None, replace=None, source_up=1.0, on_battery=None)
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "absent" in msg2
    assert src.state.down_streaks.get("ups", 0) == 2


def test_check_ups_all_absent_nut_scrape_down_still_defers(cfg):
    """A dead upsd and a dead exporter are the SAME shape: one source answers every arm.

    nut-exporter fails the whole /ups_metrics scrape when upsd is unreachable (its probes are
    tcpSocket for exactly that reason), so both outages read as all four arms absent with
    `up{job="nut"} == 0`. check_ups must DEFER — Scrape Targets and the nut pod's liveness probe
    own those between them, and paging here would double-page one of them with a misdirecting
    "renamed?" message.
    """
    src = _ups_scalars(cfg, None, None, replace=None, source_up=0.0, on_battery=None)
    ok, msg = checks.host_thermal.check_ups(cfg, src)
    assert ok and "no UPS data" in msg
    assert src.state.down_streaks.get("ups", 0) == 0


def test_check_ups_replace_battery_pages(cfg):
    # RB verdict from the self-test -> down after the streak even with a full charge / good runtime.
    src = _ups_scalars(cfg, 100, 900, replace=1.0)
    ok1, _ = checks.host_thermal.check_ups(cfg, src)
    assert ok1  # streak grace on the first cycle
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "replace-battery" in msg2


def test_check_ups_numeric_arms_absent_while_replace_reports_pages(cfg):
    """charge and runtime absent while the replace arm reports: a selective shape.

    One source cannot produce that shape for an outage: nut-exporter fails the whole
    /ups_metrics scrape, taking all four arms with it. That leaves a selective rename or a
    `--nut.vars_enable` entry dropped from the exporter's arguments, and both must page rather
    than leave the runway arms silently unmonitored.
    """
    src = _ups_scalars(cfg, None, None, replace=0.0)
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2
    assert "charge" in msg2 and "runtime" in msg2 and "absent" in msg2


def test_check_ups_partial_absence_pages_not_silently_survives(cfg):
    # charge+runtime present but the replace arm vanished (series rename) -> flag, don't monitor the
    # survivor silently. Goes through the streak (restart grace) then pages, naming the missing arm.
    src = _ups_scalars(cfg, 100, 900, replace=None)
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "absent" in msg2 and "replace-battery" in msg2


def test_check_ups_single_low_runtime_is_suppressed_then_pages(cfg):
    src = _ups_scalars(cfg, 100, 60)  # runtime 1m < 5m floor
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE default 2
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "runtime" in msg2


def test_check_ups_recovery_resets_streak(cfg):
    src = _ups_scalars(cfg, 100, 60)
    checks.host_thermal.check_ups(cfg, src)  # streak advances to 1
    assert src.state.down_streaks["ups"] == 1
    state = src.state
    src = _ups_scalars(cfg, 100, 900)  # healthy again
    src.state = state
    ok, _ = checks.host_thermal.check_ups(cfg, src)
    assert ok
    assert src.state.down_streaks.get("ups", 0) == 0


def test_check_ups_disabled_when_no_queries(cfg):
    cfg = replace(
        cfg,
        UPS_CHARGE_QUERY="",
        UPS_RUNTIME_QUERY="",
        UPS_REPLACE_QUERY="",
        UPS_ON_BATTERY_QUERY="",
    )
    ok, msg = checks.host_thermal.check_ups(cfg, FakeSources())
    assert ok and "disabled" in msg


# ── mains loss: the arm the direct NUT series made possible ──
#
# The alert path reads the direct NUT series rather than Home Assistant: HA re-exports the same
# UPS, so with HA primary the alert path would go down with the workload the UPS most obviously
# protects. These are
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


def test_check_ups_pages_on_sustained_mains_loss(cfg):
    """The check goes red off network_ups_tools_ups_status{flag="OB"},
    with the runway arms reading perfectly healthy throughout — which is what they do for most
    of a real outage."""
    src = _ups_scalars(cfg, 100, 900, on_battery=1.0)
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1  # UPS_CONSECUTIVE grace rides out a brownout
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "on battery" in msg2


def test_mains_loss_outranks_a_healthy_runway_message(cfg):
    src = _ups_scalars(cfg, 100, 900, on_battery=1.0)
    _, msg = checks.host_thermal.check_ups(cfg, src)
    assert "battery 100%" not in msg, "the outage is the report, not the runway"


def test_the_on_battery_arm_holds_its_own_streak(cfg):
    """Separate keys, for the reason check_host_temp's arms record: a brownout cycle and a
    low-runway cycle sharing one counter would page at half the intended grace."""
    src = _ups_scalars(cfg, 100, 60, on_battery=1.0)
    checks.host_thermal.check_ups(cfg, src)
    assert src.state.down_streaks.get("ups_on_battery", 0) == 1
    assert src.state.down_streaks.get("ups", 0) == 0


def test_restored_mains_clears_the_on_battery_streak(cfg):
    src = _ups_scalars(cfg, 100, 900, on_battery=1.0)
    checks.host_thermal.check_ups(cfg, src)
    assert src.state.down_streaks["ups_on_battery"] == 1
    state = src.state
    src = _ups_scalars(cfg, 100, 900, on_battery=0.0)
    src.state = state
    ok, _ = checks.host_thermal.check_ups(cfg, src)
    assert ok
    assert src.state.down_streaks.get("ups_on_battery", 0) == 0


def test_the_on_battery_series_going_missing_alone_pages(cfg):
    """The absence half of the arm: a rename of the OB series alone must not go quiet.

    The three runway arms report normally, so nothing else in the check has anything to say and
    the monitor would read green while mains-loss monitoring was off — the inert-check shape.
    The arm is in `configured`, so its absence is a partial absence and pages through the same
    UPS_CONSECUTIVE streak as a charge or runtime rename.
    """
    src = _ups_scalars(cfg, 100, 900, on_battery=None)
    ok1, msg1 = checks.host_thermal.check_ups(cfg, src)
    assert ok1 and "streak 1/2" in msg1
    ok2, msg2 = checks.host_thermal.check_ups(cfg, src)
    assert not ok2 and "on-battery" in msg2 and "absent" in msg2


def test_the_on_battery_series_present_is_not_flagged_absent(cfg):
    """The accepting half: a reporting OB series leaves the up message as it was."""
    src = _ups_scalars(cfg, 100, 900, on_battery=0.0)
    ok, msg = checks.host_thermal.check_ups(cfg, src)
    assert ok and "absent" not in msg


def test_the_on_battery_arm_is_off_when_no_query_is_configured(cfg):
    off = replace(cfg, UPS_ON_BATTERY_QUERY="")
    src = _ups_scalars(off, 100, 900, on_battery=1.0)
    ok, msg = checks.host_thermal.check_ups(off, src)
    assert ok and "battery 100%" in msg


# ── pi_pressure (Pi load / memory / disk headroom off the node-pi series) ──
