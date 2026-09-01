"""Hysteresis: a check that has just come up, or has been down once, is not yet evidence.

`down_streak` and `apply_startup_grace` are the two mechanisms, and the run_once tests here
drive them end to end. The promtail dropped-entries watchdog rides along because it is the
same shape — a counter that must be read over a window rather than instantaneously.
"""

from pathlib import Path

import pytest

import bridge_config
import bridge_streaks
import bridge_io
import check

_REPO = Path(__file__).resolve().parents[5]


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
    new_count, ok, msg = bridge_streaks.down_streak(
        count, threshold, msg_in, note, held_label=held_label
    )
    assert (new_count, ok) == (expected_count, expected_ok)
    assert msg == expected_msg


def test_apply_startup_grace_single_down_is_suppressed():
    # One down cycle (a dependency still starting after the reboot) must NOT page.
    streaks = {}
    ok, msg = check.apply_startup_grace("n8n", False, "Connection refused", 2, streaks)
    assert ok
    assert "1/2" in msg
    assert "startup/redeploy grace" in msg
    assert "Connection refused" in msg  # the real reason is preserved for the log


def test_apply_startup_grace_second_consecutive_down_pages():
    # Default GRACE_CYCLES=2: the 2nd straight down is a genuinely-dead dependency -> down.
    streaks = {}
    assert check.apply_startup_grace("n8n", False, "boom", 2, streaks)[0]
    ok, msg = check.apply_startup_grace("n8n", False, "boom", 2, streaks)
    assert not ok
    assert "boom" in msg
    assert "(2 cycles)" in msg


def test_apply_startup_grace_ok_resets_streak():
    # down, then ok -> never pages, and the streak restarts so the next down is suppressed again.
    streaks = {}
    assert check.apply_startup_grace("backup", False, "down", 2, streaks)[0]
    ok, msg = check.apply_startup_grace("backup", True, "recovered", 2, streaks)
    assert ok
    assert msg == "recovered"
    assert streaks["backup"] == 0
    ok, msg = check.apply_startup_grace("backup", False, "down again", 2, streaks)
    assert ok
    assert "1/2" in msg


def test_apply_startup_grace_streaks_are_per_name():
    # Each monitor keeps its own streak — one flapping check can't age another toward paging.
    streaks = {}
    check.apply_startup_grace("n8n", False, "x", 2, streaks)
    ok, msg = check.apply_startup_grace("arr_queue", False, "y", 2, streaks)
    assert ok
    assert "1/2" in msg  # arr_queue is on its own first cycle, not n8n's second


def test_startup_grace_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every graced name is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.STARTUP_GRACE <= names


def test_startup_grace_disjoint_from_run_once_skip_sets():
    # A graced check must reach the eval path EVERY cycle for its streak to be correct, so it
    # can't also be force-skipped by a reachability gate — STARTUP_GRACE must be disjoint from
    # every run_once skip set (else the streak wouldn't advance while the dependency was down).
    assert check.STARTUP_GRACE.isdisjoint(check.PROM_DEPENDENT)
    assert check.STARTUP_GRACE.isdisjoint(check.LOKI_DEPENDENT)
    assert check.STARTUP_GRACE.isdisjoint(check.B2_DEPENDENT)
    for deps in check.EXPORTER_DEPENDENT.values():
        assert check.STARTUP_GRACE.isdisjoint(deps)


def test_startup_grace_covers_every_ungated_reach_out_check():
    # Completeness guard (the 2026-07-14 gap: prowlarr_indexers + scrutiny were reach-out checks
    # structurally identical to the four graced ones, yet omitted). Every check that polls a live
    # app dependency via _get_json — and is NEITHER reachability-gated NOR carrying its own
    # consecutive-streak hysteresis — must be in STARTUP_GRACE, else it false-pages on the
    # weekly-reboot first cycle. A new reach-out check that skips the set trips this test, forcing
    # a conscious classify (add to STARTUP_GRACE, or to the self-hysteresis allowlist below).
    import inspect

    gated = (
        set(check.PROM_DEPENDENT) | set(check.LOKI_DEPENDENT) | set(check.B2_DEPENDENT)
    )
    for deps in check.EXPORTER_DEPENDENT.values():
        gated |= set(deps)
    # These ride out the reboot blip with their own down-streak hysteresis instead of the
    # STARTUP_GRACE mechanism (HA_CONSECUTIVE / DISCORD_CONSECUTIVE).
    self_hysteresis = {"ha_heartbeat", "discord"}
    reach_out = {
        name
        for name, _, fn in check.CHECKS
        if any(h in inspect.getsource(fn) for h in ("_get_json(", "_post_json("))
    }
    ungated = reach_out - gated - self_hysteresis
    missing = ungated - check.STARTUP_GRACE
    assert not missing, "ungated reach-out checks missing startup grace: %s" % sorted(
        missing
    )


def _wire_run_once_grace(monkeypatch, results):
    """Drive run_once with Prometheus+Loki UP and one STARTUP_GRACE check whose eval returns
    `results` in order across calls; capture the (ok, msg) pushed for it each cycle."""
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset({"n8n"}))
    monkeypatch.setattr(bridge_config, "GRACE_CYCLES", 2)
    monkeypatch.setattr(check, "_grace_streaks", {})
    seq = iter(results)
    monkeypatch.setattr(check, "CHECKS", [("n8n", "tok_n8n", lambda: next(seq))])
    pushes = []
    monkeypatch.setattr(bridge_io, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    out = []
    for _ in range(len(results)):
        check.run_once()
        out.append(next((ok, m) for t, ok, m in pushes if t == "tok_n8n"))
        pushes.clear()
    return out


def test_run_once_holds_graced_check_up_on_first_down_then_pages(monkeypatch):
    # The weekly-reboot case end to end: first cycle down (dependency mid-start) is held up with a
    # streak msg; a second straight down (dependency really gone) pages with the real reason.
    out = _wire_run_once_grace(
        monkeypatch,
        [(False, "Connection refused"), (False, "Connection refused")],
    )
    assert out[0][0] is True and "1/2" in out[0][1]
    assert out[1][0] is False and "Connection refused" in out[1][1]


def test_run_once_graced_check_recovers_without_paging(monkeypatch):
    # Down then up (the real reboot recovery) never pushes a down for the graced monitor.
    out = _wire_run_once_grace(
        monkeypatch,
        [(False, "Connection refused"), (True, "queue clean")],
    )
    assert out[0][0] is True
    assert out[1] == (True, "queue clean")


# ── promtail dropped-entries watchdog (Prometheus counter; partial log loss) ──


@pytest.mark.parametrize(
    ("count", "ok", "must_contain"),
    [
        pytest.param(50, True, ("ok",), id="under_threshold_is_ok"),
        pytest.param(
            5000, False, ("5000", "partial log loss"), id="over_threshold_is_down"
        ),
        # No series (counter never incremented) -> None -> 0 -> up.
        pytest.param(None, True, (), id="none_is_ok"),
        # Exactly at the threshold must NOT alert (strictly greater).
        pytest.param(1000, True, (), id="at_threshold_is_ok"),
    ],
)
def test_promtail_dropped(count, ok, must_contain):
    result_ok, msg = check.promtail_dropped(count, "1h", 1000)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_check_promtail_dropped_uses_increase(monkeypatch):
    queries = []

    def fake_scalar(q):
        queries.append(q)
        return 5000.0

    monkeypatch.setattr(bridge_io, "prom_scalar", fake_scalar)
    ok, _ = check.check_promtail_dropped()
    assert not ok
    # No reason filter — sums drops across ALL reasons (rate_limited/stream_limited/... too, M2).
    assert any(
        "increase(" in q and "promtail_dropped_entries_total" in q and "reason" not in q
        for q in queries
    )
