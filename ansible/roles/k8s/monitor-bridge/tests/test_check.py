"""The CHECKS registry itself: what is declared, what is selected, and the loop heartbeat.

`CHECKS` has to agree with the deployed manifests — a check needing an env var the manifest does
not set fails at runtime, and a monitor with no check never beats. CHECKS_ONLY/CHECKS_SKIP are
how a deployment narrows that registry.
"""

from dataclasses import replace

import os
from pathlib import Path


import bridge.common
from bridge.config import load_config
import bridge.streaks
import check
import gates
import registry
from _check_gate_helpers import mk
from _fake_sources import FakeSink, FakeSources
from gates import Gates


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
    assert gates.check_enabled("disk", frozenset(), frozenset())
    assert gates.check_enabled("gitops_alive", SUBSET_ONLY, frozenset())
    assert not gates.check_enabled("disk", SUBSET_ONLY, frozenset())
    assert not gates.check_enabled("disk", frozenset(), frozenset({"disk"}))
    # skip wins even against an explicit only-listing
    assert not gates.check_enabled("disk", frozenset({"disk"}), frozenset({"disk"}))


def test_name_set_parses_csv_with_spaces():
    # Read through the field, not through load_config's private parser: CHECKS_ONLY is what
    # check_enabled consumes, and a parser that stopped being called would still pass a test
    # aimed at the parser itself.
    assert load_config({"CHECKS_ONLY": " a, b ,c,,"}).CHECKS_ONLY == frozenset(
        {"a", "b", "c"}
    )
    assert load_config({"CHECKS_ONLY": ""}).CHECKS_ONLY == frozenset()


def test_validate_rejects_unknown_names():
    problems = gates.validate_check_filter(
        frozenset({"no_such_check"}), frozenset({"also_bogus"}), registry.build_checks()
    )
    assert any("no_such_check" in p for p in problems)
    assert any("also_bogus" in p for p in problems)


def test_validate_rejects_enabled_dependent_with_disabled_gate():
    # Skipping the prometheus gate while its dependents still run would reintroduce the
    # one-outage-N-page storm the gate exists to prevent.
    problems = gates.validate_check_filter(
        frozenset(), frozenset({"prometheus"}), registry.build_checks()
    )
    assert len(problems) == 1
    assert "gate prometheus is disabled" in problems[0]


def test_validate_accepts_only_and_skip_shapes():
    # Both filter directions of a gate-free subset must validate clean.
    assert (
        gates.validate_check_filter(SUBSET_ONLY, frozenset(), registry.build_checks())
        == []
    )
    assert (
        gates.validate_check_filter(frozenset(), SUBSET_ONLY, registry.build_checks())
        == []
    )


def test_subset_names_are_real_checks():
    # Guard (mirrors the PROM_DEPENDENT guard): the subset must track CHECKS renames.
    names = {c.name for c in registry.build_checks()}
    assert SUBSET_ONLY <= names


def test_run_once_with_only_filter_touches_no_gate(cfg):
    # With a CHECKS_ONLY filter active, run_once must evaluate exactly that set — no
    # gate probe, no metric check, no push for anything else.
    cfg = replace(cfg, CHECKS_ONLY=SUBSET_ONLY, CHECKS_SKIP=frozenset())
    evaluated = []
    # Every body `_evaluate` can reach is a recorder: each registry entry and each of the three
    # gate probes. That covers the whole registry, so a body outside the filter that ran
    # would be recorded, which stating a registry of only the subset could not show.
    checks = [replace(c, fn=mk(evaluated, c.name)) for c in registry.build_checks()]
    probes = Gates(
        probe_prometheus=mk(evaluated, "prometheus"),
        probe_b2=mk(evaluated, "b2_reachable"),
        probe_wan=mk(evaluated, "wan_reachable"),
    )
    sink = FakeSink()
    # No answers: the Prometheus gate is outside the filter, so the exporter probe sends nothing.
    check.run_once(cfg, FakeSources(), checks, probes, sink)
    assert set(evaluated) == SUBSET_ONLY
    assert len(sink.pushes) == len(SUBSET_ONLY)


def _quiet_cycle(cfg, checks, capsys, **kwargs):
    """One run_once over `checks` with every gate up; returns what it logged."""
    cfg = replace(cfg, CHECKS_ONLY=frozenset(), CHECKS_SKIP=frozenset())
    ran = []
    probes = Gates(
        probe_prometheus=mk(ran, "prometheus"),
        probe_b2=mk(ran, "b2_reachable"),
        probe_wan=mk(ran, "wan_reachable"),
    )
    check.run_once(cfg, FakeSources(), checks, probes, FakeSink(), **kwargs)
    return capsys.readouterr().out


def test_a_cycle_logs_down_verdicts_and_one_summary_not_each_up(cfg, capsys):
    """The pod's ~50 OK lines per cycle buried the DOWN ones (#4448)."""
    first, second = [
        e for e in registry.build_checks() if e.name not in check.LOGGED_WHEN_UP
    ][:2]
    out = _quiet_cycle(
        cfg,
        [
            replace(first, fn=mk([], first.name)),
            replace(second, fn=lambda _cfg, _src: (False, "over threshold")),
        ],
        capsys,
    )
    assert "OK  " not in out
    assert f"DOWN {second.name} - over threshold" in out
    assert "CYCLE 1 up, 1 down, 0 skipped" in out


def test_a_dry_run_still_logs_each_up_verdict(cfg, capsys):
    entry = registry.build_checks()[0]
    out = _quiet_cycle(
        cfg, [replace(entry, fn=mk([], entry.name))], capsys, dry_run=True
    )
    assert f"OK   {entry.name} - {entry.name} ok" in out
    assert "OK   prometheus" in out


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
        [replace(entry, fn=lambda _cfg, _src: (True, "B2 storage 1.0 GB of 10.0 GB"))],
        capsys,
    )
    assert "OK   b2_storage - B2 storage 1.0 GB of 10.0 GB" in out
