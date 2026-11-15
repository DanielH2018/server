"""The check exporter: what `run_once` records per row, and the row count `main()` serves.

Every row exports its verdict. The cases here pin the places a verdict is not the body's plain
result: a gate-skipped row, a `startup_grace` row (no hold since the push is gone), and a body that
raises.
"""

import os
import re
from dataclasses import replace

import pytest

import check
import verdict_export
import check_table
import cli
import deploy_pipeline
import registry
from _bridge_env import bridge_env
from _fake_sources import mk
from _fake_sources import FakeSources
from bridge.types import Check, CheckResult

SERIES = re.compile(r'^(\w+)\{check="([^"]+)"\} (\S+)$', re.MULTILINE)


def _series(exposition, metric):
    """`{check: value}` for one per-row gauge in the exposition's served body."""
    body = exposition.body().decode()
    return {c: float(v) for m, c, v in SERIES.findall(body) if m == metric}


def _cycle(cfg, wan_up):
    """One `run_once` with a WAN row, a graced row, a raising row and a plain row after it."""
    ran = []
    export = verdict_export.CheckExposition(cfg.INTERVAL)

    def graced(_cfg, _src):
        ran.append("graced")
        return False, "dependency still starting"

    def boom(_cfg, _src):
        ran.append("boom")
        raise RuntimeError("body exploded")

    check.run_once(
        cfg,
        FakeSources(prom_vector=lambda q: []),
        [
            Check("b2_reachable", lambda _cfg, _src: (True, "b2 ok")),
            Check("wan_reachable", lambda _cfg, _src: (wan_up, "wan")),
            Check("wan_row", mk(ran, "wan_row"), "wan_reachable"),
            Check("graced", graced, "startup_grace"),
            Check("boom", boom),
            Check("after", mk(ran, "after")),
        ],
        export=export,
    )
    return ran, export


def test_both_gate_rows_export_their_verdict(cfg):
    _, export = _cycle(cfg, wan_up=True)
    success = _series(export, "homelab_check_success")
    assert success["wan_reachable"] == 1.0
    assert success["b2_reachable"] == 1.0


def test_a_row_skipped_by_a_down_gate_exports_zero_without_running_its_body(cfg):
    ran, export = _cycle(cfg, wan_up=False)
    assert "wan_row" not in ran
    success = _series(export, "homelab_check_success")
    assert success["wan_reachable"] == 0.0
    assert success["wan_row"] == 0.0
    assert "wan_row" in _series(export, "homelab_check_last_run_timestamp_seconds")


def test_a_startup_grace_row_exports_its_raw_down_with_no_hold(cfg):
    """RED on a loop that still held a graced row `up`: the rule's `for: 10m` is the hold."""
    _, export = _cycle(cfg, wan_up=True)
    assert _series(export, "homelab_check_success")["graced"] == 0.0


def test_a_raising_body_exports_zero_and_the_next_row_still_runs(cfg):
    # fact: k8s/roles/monitor-bridge/CLAUDE.md#The two exporters
    ran, export = _cycle(cfg, wan_up=True)
    assert ran.index("boom") < ran.index("after")
    success = _series(export, "homelab_check_success")
    assert success["boom"] == 0.0
    assert success["after"] == 1.0


def test_every_row_carries_the_three_cycle_deadline(cfg):
    _, export = _cycle(cfg, wan_up=True)
    deadlines = _series(export, "homelab_check_deadline_seconds")
    assert deadlines
    assert set(deadlines.values()) == {3.0 * cfg.INTERVAL}


class _Slept(Exception):
    pass


def _served_rows():
    """`homelab_check_rows` from one looping `main()` cycle over the production registry."""
    env = bridge_env(HEARTBEAT_FILE=os.devnull)
    export = verdict_export.CheckExposition(300)
    wired = {
        "env": env,
        # The two gate probes reach the network by their own transport, so they are stated.
        "checks": [
            replace(c, fn=lambda _cfg, _src: (False, "stub"))
            if c.name in check.GATE_SKIP
            else c
            for c in registry.build_checks()
        ],
        "sources": FakeSources(prom_vector=lambda q: []),
        "exposition": deploy_pipeline.Exposition(),
        "check_exposition": export,
    }

    def sleep(_seconds):
        raise _Slept

    with pytest.raises(_Slept):
        cli.main([], sleep=sleep, **wired)
    (rows,) = re.findall(r"^homelab_check_rows (\d+)$", export.body().decode(), re.M)
    return int(rows), wired["checks"]


def test_the_row_count_is_the_table_gate_rows_included():
    rows, built = _served_rows()
    names = {row.name for row in check_table.CHECKS}
    assert rows == len(names)
    assert {"wan_reachable", "b2_reachable"} <= names
    assert "prometheus" not in names
    # Red on an exporter that counts anything but the table: the registry is the table's length.
    assert len(built) == rows


DETAIL = re.compile(r'^homelab_check_detail\{check="([^"]+)",detail="(.*)"\} 1$', re.M)


def _details(exposition):
    """`(check, detail)` for every `homelab_check_detail` series in the served body."""
    return DETAIL.findall(exposition.body().decode())


def test_a_down_row_serves_its_members_and_an_up_row_serves_none(cfg):
    export = verdict_export.CheckExposition(cfg.INTERVAL)
    check.run_once(
        cfg,
        FakeSources(prom_vector=lambda q: []),
        [
            Check(
                "down", lambda _c, _s: CheckResult(False, "2 bad", ["zeta", "alpha"])
            ),
            Check("held", lambda _c, _s: CheckResult(True, "streak 1/2", ["alpha"])),
        ],
        export=export,
    )
    assert _details(export) == [("down", "alpha"), ("down", "zeta")]


def test_a_recovered_row_drops_its_detail_series(cfg):
    export = verdict_export.CheckExposition(cfg.INTERVAL)
    export.record("row", False, 0.0, ("EZTV",))
    assert _details(export) == [("row", "EZTV")]
    export.record("row", True, 300.0, ("EZTV",))
    assert _details(export) == []
    assert "homelab_check_detail" not in export.body().decode()


def test_a_detail_label_value_is_escaped():
    export = verdict_export.CheckExposition(300)
    export.record("row", False, 0.0, ('say "hi" \\ there',))
    assert _details(export) == [("row", 'say \\"hi\\" \\\\ there')]


def test_a_check_result_unpacks_as_a_pair_and_bounds_its_details():
    many = ["m%d" % i for i in range(9, -1, -1)] + ["m3", "  ", "x" * 100]
    result = CheckResult(False, "msg", many)
    ok, msg = result
    assert (ok, msg) == (False, "msg") == result
    assert len(result.details) == CheckResult.DETAILS_MAX
    assert result.details == tuple(sorted(set(result.details)))
    assert all(len(d) <= CheckResult.DETAIL_MAXLEN for d in result.details)
    assert CheckResult.of((True, "plain")).details == ()
