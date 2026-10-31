"""Hysteresis: a check that has just come up, or has been down once, is not yet evidence.

`down_streak` and `apply_startup_grace` are the two mechanisms, and the run_once tests here
drive them end to end.
"""

from dataclasses import replace

import pytest

import bridge.config
import bridge.streaks
import check
import registry
from _fake_sources import FakeSink, FakeSources
from bridge.streaks import State
from bridge.types import Check


@pytest.mark.parametrize(
    (
        "count",
        "threshold",
        "msg_in",
        "note",
        "held_label",
        "expected_count",
        "expected_ok",
        "expected_msg",
    ),
    [
        pytest.param(
            0,
            2,
            "boom",
            "grace",
            "down streak",
            1,
            True,
            "down streak 1/2 (grace): boom",
            id="holds_up_below_threshold",
        ),
        pytest.param(
            1,
            2,
            "boom",
            "grace",
            "down streak",
            2,
            False,
            "boom (2 cycles)",
            id="pages_at_threshold",
        ),
        pytest.param(
            0,
            3,
            "x",
            "not alerting yet",
            "throttling streak",
            1,
            True,
            "throttling streak 1/3 (not alerting yet): x",
            id="custom_label_and_note",
        ),
    ],
)
def test_down_streak(
    count,
    threshold,
    msg_in,
    note,
    held_label,
    expected_count,
    expected_ok,
    expected_msg,
):
    new_count, ok, msg = bridge.streaks.down_streak(
        count, threshold, msg_in, note, held_label=held_label
    )
    assert (new_count, ok) == (expected_count, expected_ok)
    assert msg == expected_msg


def test_apply_startup_grace_single_down_is_suppressed():
    # One down cycle (a dependency still starting after the reboot) must NOT page.
    streaks = {}
    ok, msg = bridge.streaks.apply_startup_grace(
        "n8n", False, "Connection refused", 2, streaks
    )
    assert ok
    assert "1/2" in msg
    assert "startup/redeploy grace" in msg
    assert "Connection refused" in msg  # the real reason is preserved for the log


def test_apply_startup_grace_second_consecutive_down_pages():
    # Default GRACE_CYCLES=2: the 2nd straight down is a genuinely-dead dependency -> down.
    streaks = {}
    assert bridge.streaks.apply_startup_grace("n8n", False, "boom", 2, streaks)[0]
    ok, msg = bridge.streaks.apply_startup_grace("n8n", False, "boom", 2, streaks)
    assert not ok
    assert "boom" in msg
    assert "(2 cycles)" in msg


def test_apply_startup_grace_ok_resets_streak():
    # down, then ok -> never pages, and the streak restarts so the next down is suppressed again.
    streaks = {}
    assert bridge.streaks.apply_startup_grace("backup", False, "down", 2, streaks)[0]
    ok, msg = bridge.streaks.apply_startup_grace(
        "backup", True, "recovered", 2, streaks
    )
    assert ok
    assert msg == "recovered"
    assert streaks["backup"] == 0
    ok, msg = bridge.streaks.apply_startup_grace(
        "backup", False, "down again", 2, streaks
    )
    assert ok
    assert "1/2" in msg


def test_apply_startup_grace_streaks_are_per_name():
    # Each monitor keeps its own streak — one flapping check can't age another toward paging.
    streaks = {}
    bridge.streaks.apply_startup_grace("n8n", False, "x", 2, streaks)
    ok, msg = bridge.streaks.apply_startup_grace("arr_queue", False, "y", 2, streaks)
    assert ok
    assert "1/2" in msg  # arr_queue is on its own first cycle, not n8n's second


def _gated(gate):
    """The registry's rows whose `gate` column reads `gate`."""
    return {c.name for c in registry.build_checks() if c.gate == gate}


def test_startup_grace_set_matches_real_checks():
    # Non-vacuity: the set is the table's column, and it is not empty.
    assert {"n8n", "bazarr", "scrutiny", "speedtest"} <= _gated(check.STARTUP_GRACE)


def test_startup_grace_covers_every_ungated_reach_out_check():
    # Completeness guard. Every check that polls a live app dependency via _get_json — and is
    # NEITHER reachability-gated NOR carrying its own consecutive-streak hysteresis — must be
    # `startup_grace`, else it false-pages on the weekly-reboot first cycle. A new reach-out
    # check that skips the column trips this test, forcing a conscious classify (give it the
    # value, or add it to the self-hysteresis allowlist below). One value per row keeps a graced
    # row out of every skip set, so its streak advances while the dependency is down.
    import inspect

    exporter_fed = set().union(*check.EXPORTER_DEPENDENT.values())
    # These ride out the reboot blip with their own down-streak hysteresis instead of the
    # startup grace (HA_CONSECUTIVE / DISCORD_CONSECUTIVE / ARR_FETCH_CONSECUTIVE).
    self_hysteresis = {"ha_heartbeat", "discord", "arr_queue"}
    reach_out = {
        c.name
        for c in registry.build_checks()
        if any(h in inspect.getsource(c.fn) for h in ("_get_json(", "_post_json("))
    }
    ungraced = {
        c.name
        for c in registry.build_checks()
        if c.name in reach_out
        and c.name
        not in check.GATE_SKIP  # a gate row is the gate, not a row behind one
        and c.gate is None
        and c.name not in exporter_fed | self_hysteresis
    }
    assert not ungraced, "ungated reach-out checks missing startup grace: %s" % sorted(
        ungraced
    )


def _wire_run_once_grace(cfg, results):
    """Drive run_once with Prometheus UP and one STARTUP_GRACE check whose eval returns
    `results` in order across calls; capture the (ok, msg) pushed for it each cycle."""
    cfg = replace(cfg, GRACE_CYCLES=2)
    seq = iter(results)
    checks = [
        Check("n8n", "tok_n8n", lambda _cfg, _src: next(seq), check.STARTUP_GRACE)
    ]
    # Each cycle builds its own fake, so the streak survives only through the ONE `State` they
    # share, as it survives in the pod through the one `Sources` main() builds.
    state = State()
    out = []
    for _ in range(len(results)):
        sink = FakeSink()
        check.run_once(
            cfg,
            FakeSources(state=state, prom_vector=lambda q: []),
            checks,
            sink,
        )
        out.append(next((ok, m) for t, ok, m in sink.pushes if t == "tok_n8n"))
    return out


def test_run_once_holds_graced_check_up_on_first_down_then_pages(cfg):
    # The weekly-reboot case end to end: first cycle down (dependency mid-start) is held up with a
    # streak msg; a second straight down (dependency really gone) pages with the real reason.
    out = _wire_run_once_grace(
        cfg,
        [(False, "Connection refused"), (False, "Connection refused")],
    )
    assert out[0][0] is True and "1/2" in out[0][1]
    assert out[1][0] is False and "Connection refused" in out[1][1]


def test_run_once_graced_check_recovers_without_paging(cfg):
    # Down then up (the real reboot recovery) never pushes a down for the graced monitor.
    out = _wire_run_once_grace(
        cfg,
        [(False, "Connection refused"), (True, "queue clean")],
    )
    assert out[0][0] is True
    assert out[1] == (True, "queue clean")
