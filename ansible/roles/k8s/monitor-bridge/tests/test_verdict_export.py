"""The check exporter: what `run_once` records per row, and the row count `main()` serves.

Until the push path is deleted, every row exports the end state's verdict beside the push that
keeps today's value. The cases here pin the places the two differ: a gate-skipped row, a row held
`up` by `startup_grace`, and a body that raises.
"""

import os
import re

import pytest

import check
import verdict_export
import check_table
import cli
import deploy_pipeline
import registry
from _bridge_env import bridge_env
from _check_gate_helpers import mk
from _fake_sources import FakeSink, FakeSources
from bridge.types import Check
from gates import Gates

SERIES = re.compile(r'^(\w+)\{check="([^"]+)"\} (\S+)$', re.MULTILINE)


def _series(exposition, metric):
    """`{check: value}` for one per-row gauge in the exposition's served body."""
    body = exposition.body().decode()
    return {c: float(v) for m, c, v in SERIES.findall(body) if m == metric}


def _cycle(cfg, wan_up):
    """One `run_once` with a WAN row, a graced row, a raising row and a plain row after it."""
    ran, sink = [], FakeSink()
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
            Check("wan_row", "tok_wan_row", mk(ran, "wan_row")),
            Check("graced", "tok_graced", graced),
            Check("boom", "tok_boom", boom),
            Check("after", "tok_after", mk(ran, "after")),
        ],
        gates=Gates(
            prom_dependent=frozenset(),
            b2_dependent=frozenset(),
            wan_dependent=frozenset({"wan_row"}),
            startup_grace=frozenset({"graced"}),
            probe_prometheus=lambda _cfg, _src: (True, "prom ok"),
            probe_b2=lambda _cfg, _src: (True, "b2 ok"),
            probe_wan=lambda _cfg, _src: (wan_up, "wan %s" % wan_up),
        ),
        sink=sink,
        export=export,
    )
    pushed = {t: (ok, msg) for t, ok, msg in sink.pushes if t.startswith("tok_")}
    return ran, pushed, export


def test_both_gate_rows_export_their_verdict(cfg):
    _, _, export = _cycle(cfg, wan_up=True)
    success = _series(export, "homelab_check_success")
    assert success["wan_reachable"] == 1.0
    assert success["b2_reachable"] == 1.0


def test_a_row_skipped_by_a_down_gate_exports_zero_while_the_push_stays_up(cfg):
    ran, pushed, export = _cycle(cfg, wan_up=False)
    assert "wan_row" not in ran
    ok, msg = pushed["tok_wan_row"]
    assert ok and msg.startswith("skipped")
    success = _series(export, "homelab_check_success")
    assert success["wan_reachable"] == 0.0
    assert success["wan_row"] == 0.0
    assert "wan_row" in _series(export, "homelab_check_last_run_timestamp_seconds")


def test_a_graced_row_exports_the_raw_down_while_the_push_holds_it_up(cfg):
    _, pushed, export = _cycle(cfg, wan_up=True)
    ok, msg = pushed["tok_graced"]
    assert ok and "1/2" in msg
    assert _series(export, "homelab_check_success")["graced"] == 0.0


def test_a_raising_body_exports_zero_and_the_next_row_still_runs(cfg):
    ran, _, export = _cycle(cfg, wan_up=True)
    assert ran.index("boom") < ran.index("after")
    success = _series(export, "homelab_check_success")
    assert success["boom"] == 0.0
    assert success["after"] == 1.0


def test_every_row_carries_the_three_cycle_deadline(cfg):
    _, _, export = _cycle(cfg, wan_up=True)
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
        "checks": registry.build_checks(env),
        "sources": FakeSources(prom_vector=lambda q: []),
        "sink": FakeSink(),
        "exposition": deploy_pipeline.Exposition(),
        "check_exposition": export,
        "gate_config": Gates(
            probe_prometheus=lambda _cfg, _src: (False, "stub"),
            probe_wan=lambda _cfg, _src: (False, "stub"),
            probe_b2=lambda _cfg, _src: (False, "stub"),
        ),
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
    # Red on an exporter that counts `registry.build_checks`, which drops every gate row.
    assert {"wan_reachable", "b2_reachable", "prometheus"} <= names
    assert len(built) < rows
