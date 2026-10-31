"""The run loop: what a registry declares and selects, how a gate row skips its dependents, and the heartbeat.

`CHECKS` has to agree with the deployed manifests — a check needing an env var the manifest does
not set fails at runtime, and a monitor with no check never beats. CHECKS_ONLY/CHECKS_SKIP are
how a deployment narrows that registry.

A monitor that cannot reach Prometheus, B2 or the WAN must report "cannot tell", never "down" —
otherwise one unreachable dependency fires every monitor that reads it at once. These tests drive
`run_once()` end to end with a fake source and sink, so they fail when the wiring changes rather
than when the logic does. Each states the registry it means as `Check` entries, a gate row first
and the rows that name it after. Three neighbours own the rest: the gate column's membership is
`test_check_table.py`, the exporter suppression is `test_check_exporters.py`, and the startup
grace is `test_check_streaks.py`.
"""

from dataclasses import replace

import os
import re
from pathlib import Path

import pytest

import bridge.common
from bridge.config import load_config
import check
import registry
import verdict_export
from _fake_sources import FakeSink, FakeSources, as_body, mk
from bridge.types import Check


# ── loop heartbeat (container healthcheck reads this file's mtime) ─────────────


def test_touch_heartbeat_writes_and_refreshes(tmp_path, cfg):
    hb = tmp_path / "heartbeat"
    cfg = replace(cfg, HEARTBEAT_FILE=str(hb))
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.exists()
    first = hb.stat().st_mtime
    os.utime(hb, (first - 100, first - 100))  # backdate, then refresh
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.stat().st_mtime > first - 100


def test_touch_heartbeat_never_raises(cfg):
    # Best-effort like push(): a heartbeat failure must not kill the loop.
    cfg = replace(cfg, HEARTBEAT_FILE="/nonexistent-dir/heartbeat")
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)


# A representative CHECKS_ONLY subset: only the host-state-file checks, every gate off. No
# deployment carries a filter — these tests keep the MECHANISM honest for whenever a split is
# next expressed.
SUBSET_ONLY = frozenset({"gitops_alive", "gitops_status"})


def test_check_enabled_only_and_skip_semantics():
    assert check.check_enabled("disk", frozenset(), frozenset())
    assert check.check_enabled("gitops_alive", SUBSET_ONLY, frozenset())
    assert not check.check_enabled("disk", SUBSET_ONLY, frozenset())
    assert not check.check_enabled("disk", frozenset(), frozenset({"disk"}))
    # skip wins even against an explicit only-listing
    assert not check.check_enabled("disk", frozenset({"disk"}), frozenset({"disk"}))


def test_name_set_parses_csv_with_spaces():
    # Read through the field, not through load_config's private parser: CHECKS_ONLY is what
    # check_enabled consumes, and a parser that stopped being called would still pass a test
    # aimed at the parser itself.
    assert load_config({"CHECKS_ONLY": " a, b ,c,,"}).CHECKS_ONLY == frozenset(
        {"a", "b", "c"}
    )
    assert load_config({"CHECKS_ONLY": ""}).CHECKS_ONLY == frozenset()


def test_subset_names_are_real_checks():
    # Guard: the subset must track CHECKS renames.
    names = {c.name for c in registry.build_checks()}
    assert SUBSET_ONLY <= names


def test_run_once_with_only_filter_touches_no_gate(cfg):
    # With a CHECKS_ONLY filter active, run_once must evaluate exactly that set — no
    # gate probe, no metric check, no push for anything else.
    cfg = replace(cfg, CHECKS_ONLY=SUBSET_ONLY, CHECKS_SKIP=frozenset())
    evaluated = []
    # Every body `_evaluate` can reach is a recorder: each registry entry, gate rows included.
    # That covers the whole registry, so a body outside the filter that ran would be recorded,
    # which stating a registry of only the subset could not show.
    checks = [replace(c, fn=mk(evaluated, c.name)) for c in registry.build_checks()]
    sink = FakeSink()
    # No answers: the Prometheus gate is outside the filter, so the exporter probe sends nothing.
    check.run_once(cfg, FakeSources(), checks, sink)
    assert set(evaluated) == SUBSET_ONLY
    assert len(sink.pushes) == len(SUBSET_ONLY)


def _quiet_cycle(cfg, checks, capsys, **kwargs):
    """One run_once over `checks`; returns what it logged."""
    cfg = replace(cfg, CHECKS_ONLY=frozenset(), CHECKS_SKIP=frozenset())
    check.run_once(cfg, FakeSources(), checks, FakeSink(), **kwargs)
    return capsys.readouterr().out


def test_a_cycle_logs_down_verdicts_and_one_summary_not_each_up(cfg, capsys):
    """The pod's ~50 OK lines per cycle buried the DOWN ones (#4448)."""
    first, second = [
        e
        for e in registry.build_checks()
        if e.name not in check.LOGGED_WHEN_UP and e.name not in check.GATE_SKIP
    ][:2]
    out = _quiet_cycle(
        cfg,
        [
            replace(first, fn=mk([], first.name), gate=None),
            replace(second, fn=lambda _cfg, _src: (False, "over threshold"), gate=None),
        ],
        capsys,
    )
    assert "OK  " not in out
    assert f"DOWN {second.name} - over threshold" in out
    assert "CYCLE 1 up, 1 down, 0 skipped" in out


def test_a_dry_run_still_logs_each_up_verdict(cfg, capsys):
    entry = next(e for e in registry.build_checks() if e.name == "gitops_alive")
    out = _quiet_cycle(
        cfg, [replace(entry, fn=mk([], entry.name))], capsys, dry_run=True
    )
    assert f"OK   {entry.name} - {entry.name} ok" in out


def test_every_dashboard_parsed_check_is_logged_when_up(cfg, capsys):
    """The Longhorn Storage B2 panels regex b2_storage's up line out of this pod's log."""
    board = (
        Path(__file__).resolve().parents[2]
        / "observability/files/dashboards/Infrastructure/longhorn-storage.json"
    ).read_text()
    assert '{container=\\"monitor-bridge\\"} |= \\"B2 storage\\"' in board
    entry = next(e for e in registry.build_checks() if e.name == "b2_storage")
    out = _quiet_cycle(
        cfg,
        [
            replace(
                entry,
                fn=lambda _cfg, _src: (True, "B2 storage 1.0 GB of 10.0 GB"),
                gate=None,
            )
        ],
        capsys,
    )
    assert "OK   b2_storage - B2 storage 1.0 GB of 10.0 GB" in out


# ── the registry: gate rows first, each row carrying its gate ───────────────────────────────


def test_the_registry_runs_the_gate_rows_first_and_hands_each_row_its_gate():
    checks = registry.build_checks({})
    gates = [c.name for c in checks[:3]]
    assert gates == ["prometheus", "b2_reachable", "wan_reachable"]
    by_name = {c.name: c for c in checks}
    assert by_name["b2_storage"].gate == "b2_reachable"
    assert by_name["discord"].gate == "wan_reachable"
    assert by_name["n8n"].gate == check.STARTUP_GRACE
    assert by_name["gitops_alive"].gate is None
    assert all(by_name[g].gate is None for g in gates)


# ── a gate row skips the rows that name it ──────────────────────────────────────────────────


def _gated_cycle(cfg, gate, result, **kwargs):
    """One cycle: `gate` as a row reporting `result`, a row behind it, and one behind nothing.

    Returns (ran, pushed): the names whose body ran, and {token: (ok, msg)} for every push.
    """
    ran = []
    sink = FakeSink()
    check.run_once(
        cfg,
        FakeSources(prom_vector=lambda q: []),
        [
            Check(gate, "tok_gate", as_body(result)),
            Check("behind", "tok_behind", mk(ran, "behind"), gate),
            Check("free", "tok_free", mk(ran, "free")),
        ],
        sink,
        **kwargs,
    )
    return ran, {tok: (ok, msg) for tok, ok, msg in sink.pushes}


@pytest.mark.parametrize("gate", sorted(check.GATE_SKIP))
def test_a_down_gate_skips_its_dependents_and_pages_once(cfg, gate):
    """One page for the outage, not one per dependent — and not zero."""
    ran, pushed = _gated_cycle(cfg, gate, (False, "gate says down"))
    assert ran == ["free"]
    # The dependent's heartbeat survives with the gate's skip message; the gate's own tile pages.
    assert pushed["tok_behind"] == (True, check.GATE_SKIP[gate])
    assert pushed["tok_gate"] == (False, "gate says down")


@pytest.mark.parametrize("gate", sorted(check.GATE_SKIP))
def test_a_gate_that_is_up_skips_nothing(cfg, gate):
    ran, pushed = _gated_cycle(cfg, gate, (True, "gate says up"))
    assert ran == ["behind", "free"]
    assert pushed["tok_behind"] == (True, "behind ok")


def test_a_raising_gate_probe_is_a_down_gate(cfg):
    """The real outage path: the probe raises rather than returning a pair."""
    ran, pushed = _gated_cycle(
        cfg, "wan_reachable", RuntimeError("name resolution failed")
    )
    assert ran == ["free"]
    assert pushed["tok_gate"][0] is False
    assert "name resolution failed" in pushed["tok_gate"][1]


def test_a_gate_the_filter_disables_reads_as_up(cfg):
    """A gate that does not run suppresses nothing, which is what lets `--check` name one row."""
    ran, pushed = _gated_cycle(
        replace(cfg, CHECKS_SKIP=frozenset({"wan_reachable"})),
        "wan_reachable",
        (False, "would be down"),
    )
    assert ran == ["behind", "free"]
    assert "tok_gate" not in pushed


def test_a_gate_row_exports_its_verdict_and_a_skipped_row_exports_zero(cfg):
    export = verdict_export.CheckExposition(cfg.INTERVAL)
    _gated_cycle(cfg, "b2_reachable", (False, "capped"), export=export)
    body = export.body().decode()
    for name, value in (("b2_reachable", 0), ("behind", 0), ("free", 1)):
        assert re.search(
            r'homelab_check_success\{check="%s"\} %d(\.0)?$' % (name, value),
            body,
            re.M,
        ), name


# ── the sources and the sink are injected values, not lookups ───────────────────────────────


def test_run_once_hands_its_sources_to_every_row_and_pushes_through_its_sink(cfg):
    """A body that looked its sources up elsewhere would answer from the live transport, and a
    push that bypassed the sink would reach the real Kuma."""
    src = FakeSources(prom_vector=lambda q: [])
    received = []

    def body(_cfg, got):
        received.append(got)
        return True, "ok"

    sink = FakeSink()
    check.run_once(
        cfg,
        src,
        [Check("prometheus", "tok_prom", body), Check("disk", "tok_disk", body)],
        sink,
    )
    assert len(received) == 2
    assert all(got is src for got in received)
    assert src.queries("prom_vector") == ["up"]
    assert sink.pushes == [("tok_prom", True, "ok"), ("tok_disk", True, "ok")]


def test_run_once_requires_its_sources_and_its_sink(cfg):
    """The red-proof half: no default `Sources` or `Sink`, so a forgotten argument cannot reach
    the network or push to the live Kuma."""
    with pytest.raises(TypeError):
        check.run_once(cfg, checks=[], sink=FakeSink())  # ty: ignore[missing-argument]
    with pytest.raises(TypeError):
        check.run_once(cfg, FakeSources(), [])  # ty: ignore[missing-argument]
