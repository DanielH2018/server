"""Hysteresis: a check that has just come up, or has been down once, is not yet evidence.

`down_streak` and `apply_startup_grace` are the two mechanisms, and the run_once tests here
drive them end to end. The log-shipper dropped-entries watchdog rides along because it is the
same shape — a counter that must be read over a window rather than instantaneously.
"""

from pathlib import Path

from dataclasses import replace

import pytest

import bridge.config
import bridge.streaks
import bridge.net
import checks.logs
import check
import gates
import registry
from bridge.types import Check
from gates import Gates

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


def test_startup_grace_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every graced name is a real check.
    names = {c.name for c in registry.build_checks()}
    assert gates.STARTUP_GRACE <= names


def test_startup_grace_disjoint_from_run_once_skip_sets():
    # A graced check must reach the eval path EVERY cycle for its streak to be correct, so it
    # can't also be force-skipped by a reachability gate — STARTUP_GRACE must be disjoint from
    # every run_once skip set (else the streak wouldn't advance while the dependency was down).
    assert gates.STARTUP_GRACE.isdisjoint(gates.PROM_DEPENDENT)
    assert gates.STARTUP_GRACE.isdisjoint(gates.LOKI_DEPENDENT)
    assert gates.STARTUP_GRACE.isdisjoint(gates.B2_DEPENDENT)
    for deps in gates.EXPORTER_DEPENDENT.values():
        assert gates.STARTUP_GRACE.isdisjoint(deps)


def test_startup_grace_covers_every_ungated_reach_out_check():
    # Completeness guard (the 2026-07-14 gap: prowlarr_indexers + scrutiny were reach-out checks
    # structurally identical to the four graced ones, yet omitted). Every check that polls a live
    # app dependency via _get_json — and is NEITHER reachability-gated NOR carrying its own
    # consecutive-streak hysteresis — must be in STARTUP_GRACE, else it false-pages on the
    # weekly-reboot first cycle. A new reach-out check that skips the set trips this test, forcing
    # a conscious classify (add to STARTUP_GRACE, or to the self-hysteresis allowlist below).
    import inspect

    gated = (
        set(gates.PROM_DEPENDENT) | set(gates.LOKI_DEPENDENT) | set(gates.B2_DEPENDENT)
    )
    for deps in gates.EXPORTER_DEPENDENT.values():
        gated |= set(deps)
    # These ride out the reboot blip with their own down-streak hysteresis instead of the
    # STARTUP_GRACE mechanism (HA_CONSECUTIVE / DISCORD_CONSECUTIVE).
    self_hysteresis = {"ha_heartbeat", "discord"}
    reach_out = {
        name
        for name, _, fn in ((c.name, c.token, c.fn) for c in registry.build_checks())
        if any(h in inspect.getsource(fn) for h in ("_get_json(", "_post_json("))
    }
    ungated = reach_out - gated - self_hysteresis
    missing = ungated - gates.STARTUP_GRACE
    assert not missing, "ungated reach-out checks missing startup grace: %s" % sorted(
        missing
    )


def _wire_run_once_grace(cfg, monkeypatch, results):
    """Drive run_once with Prometheus+Loki UP and one STARTUP_GRACE check whose eval returns
    `results` in order across calls; capture the (ok, msg) pushed for it each cycle."""
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, q: [])
    cfg = replace(cfg, GRACE_CYCLES=2)
    seq = iter(results)
    checks = [Check("n8n", "tok_n8n", lambda _cfg: next(seq))]
    # The streak dict is STATED rather than patched onto bridge.streaks: it is one of the eleven
    # `Gates` fields, so passing an empty one here is both the isolation this needs and a read of
    # the seam.
    gate_config = Gates(
        prom_dependent=frozenset(),
        loki_dependent=frozenset(),
        startup_grace=frozenset({"n8n"}),
        grace_streaks={},
        probe_prometheus=lambda _cfg: (True, "prom ok"),
        probe_loki=lambda _cfg: (True, "loki ok"),
    )
    pushes = []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, t, ok, m: pushes.append((t, ok, m))
    )
    out = []
    for _ in range(len(results)):
        check.run_once(cfg, checks, gates=gate_config)
        out.append(next((ok, m) for t, ok, m in pushes if t == "tok_n8n"))
        pushes.clear()
    return out


def test_run_once_holds_graced_check_up_on_first_down_then_pages(monkeypatch, cfg):
    # The weekly-reboot case end to end: first cycle down (dependency mid-start) is held up with a
    # streak msg; a second straight down (dependency really gone) pages with the real reason.
    out = _wire_run_once_grace(
        cfg,
        monkeypatch,
        [(False, "Connection refused"), (False, "Connection refused")],
    )
    assert out[0][0] is True and "1/2" in out[0][1]
    assert out[1][0] is False and "Connection refused" in out[1][1]


def test_run_once_graced_check_recovers_without_paging(monkeypatch, cfg):
    # Down then up (the real reboot recovery) never pushes a down for the graced monitor.
    out = _wire_run_once_grace(
        cfg,
        monkeypatch,
        [(False, "Connection refused"), (True, "queue clean")],
    )
    assert out[0][0] is True
    assert out[1] == (True, "queue clean")


# ── log-shipper dropped-entries watchdog (Prometheus counter; partial log loss) ──
# #993: the client-side shipper counter and the server-side Loki-discard counter are read
# together, and the LARGER of the two decides the verdict — see shipper_dropped()'s docstring
# for the measured 161,573-vs-1,027 burst that motivated this.


@pytest.mark.parametrize(
    ("client_count", "server_reasons", "ok", "must_contain"),
    [
        pytest.param(50, [], True, ("ok",), id="both_sides_under_threshold_is_clean"),
        pytest.param(
            5000,
            [],
            False,
            ("5000", "partial log loss"),
            id="client_side_alone_over_threshold_is_flagged",
        ),
        # No series on either side (counters never incremented) -> None/[] -> 0 -> up.
        pytest.param(None, [], True, (), id="no_series_either_side_is_clean"),
        # Exactly at the threshold must NOT alert (strictly greater).
        pytest.param(1000, [], True, (), id="at_threshold_is_clean"),
        # Server-side alone crosses the threshold while the client stays clean — the #993 case:
        # the client counted 1,027 (well under threshold) while Loki discarded 161,573
        # server-side. The client count here must be below `threshold` on its own, or this
        # case only proves message attribution, not that the server arm can fire by itself.
        pytest.param(
            50,
            [("too_far_behind", 161608.0), ("ingester_error", 40.0)],
            False,
            ("161608", "too_far_behind", "server-side"),
            id="server_side_alone_over_threshold_names_the_reason_is_flagged",
        ),
        # Server side has series but stays under threshold on both arms -> clean, and the
        # reason breakdown does not leak into an ok message.
        pytest.param(
            50,
            [("rate_limited", 10.0)],
            True,
            ("ok",),
            id="server_side_present_but_under_threshold_is_clean",
        ),
    ],
)
def test_shipper_dropped(client_count, server_reasons, ok, must_contain):
    result_ok, msg = checks.logs.shipper_dropped(
        client_count, server_reasons, "1h", 1000
    )
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_check_shipper_dropped_reads_both_shippers_counters(monkeypatch, cfg):
    """One scalar query over a __name__ regex, no reason filter, for the CLIENT side.

    Both estates ship through Alloy and share `loki_write_dropped_entries_total`, but the
    query stays a name regex: while daniel-pi ran Promtail its counter had a different name,
    and a selector naming only one estate's counter read the other as "0 dropped" forever —
    the same fail-open shape as a selector on a label nothing emits. No reason filter: every
    reason is a real drop (M2).
    """
    queries = []

    def fake_scalar(_cfg, q):
        queries.append(q)
        return 5000.0

    def fake_vector(_cfg, q):
        queries.append(q)
        return []

    monkeypatch.setattr(bridge.net, "prom_scalar", fake_scalar)
    monkeypatch.setattr(bridge.net, "prom_vector", fake_vector)
    ok, _ = checks.logs.check_shipper_dropped(cfg)
    assert not ok
    assert any(
        "increase(" in q
        and '{__name__=~"' in q
        and "loki_write_dropped_entries_total" in q
        and "reason" not in q
        for q in queries
    )


def test_check_shipper_dropped_reads_server_side_by_reason(monkeypatch, cfg):
    """The SERVER-side arm queries Loki's own discard counter, grouped `by (reason)` (#993).

    Grouping by reason is what lets a fired alert name the cause (too_far_behind vs a
    throughput/limit reason) instead of just a bare count.
    """
    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, q: 0.0)

    def fake_vector(_cfg, q):
        assert "sum by (reason)" in q
        assert '{__name__=~"' in q
        assert "loki_discarded_samples_total" in q
        return [({"reason": "too_far_behind"}, 161608.0)]

    monkeypatch.setattr(bridge.net, "prom_vector", fake_vector)
    ok, msg = checks.logs.check_shipper_dropped(cfg)
    assert not ok
    assert "too_far_behind" in msg
