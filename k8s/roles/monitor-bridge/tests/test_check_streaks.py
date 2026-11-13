"""Hysteresis: a check that has just come up, or has been down once, is not yet evidence.

`down_streak` is the one mechanism that holds a row in the bridge. The `startup_grace` rows hold
nothing there since A6's Cutover-2: their rules carry `for: 10m`.
"""

import pytest

import bridge.config
import bridge.streaks
import check
import registry


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


def _gated(gate):
    """The registry's rows whose `gate` column reads `gate`."""
    return {c.name for c in registry.build_checks() if c.gate == gate}


def test_startup_grace_set_matches_real_checks():
    # Non-vacuity: the set is the table's column, and it is not empty.
    assert {"n8n", "bazarr", "scrutiny", "speedtest"} <= _gated("startup_grace")


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
