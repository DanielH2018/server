"""The run-loop gates: what suppresses a check rather than what the check decides.

A monitor that cannot reach Prometheus, Loki or B2 must report "cannot tell", never "down" —
otherwise one unreachable dependency fires every monitor that reads it at once.

These are the tests that drive `run_once()` end to end with the transport stubbed, so they are
the ones that fail when the wiring changes rather than the logic. Each states the gate
configuration it means as a `Gates(...)` value; the drivers are `_check_gate_helpers.py`.

Three neighbours own the rest of what this file used to carry: membership of the gate sets is
`test_check_gate_dependents.py`, the two cluster-Prometheus checks' verdicts are
`test_check_k8s_workload_gates.py`, and the `origin` pin is `test_check_origin_pinning.py`.
"""

from dataclasses import replace

import pytest

import bridge.net
import bridge.parsing
import check
import checks.cluster
import checks.logs
from _check_gate_helpers import run_once_with_gates, wire_run_once, wire_run_once_loki
from bridge.types import Check
from gates import Gates


def test_loki_reachable_ok(monkeypatch, cfg):
    monkeypatch.setattr(
        bridge.net, "_get_json", lambda *a, **k: {"status": "success", "data": ["job"]}
    )
    assert bridge.net.loki_reachable(cfg) is True
    ok, msg = checks.logs.check_loki_reachable(cfg)
    assert ok
    assert "reachable" in msg.lower()


def test_loki_reachable_non_success_raises(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        bridge.net.loki_reachable(cfg)


# ── Prometheus reachability gate + alert-storm suppression (L1) ──────────────


def test_check_prometheus_reachable(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, q: 1.0)
    ok, msg = checks.cluster.check_prometheus(cfg)
    assert ok
    assert "reachable" in msg.lower()


def test_check_prometheus_no_data_is_down(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, q: None)
    ok, _msg = checks.cluster.check_prometheus(cfg)
    assert not ok


def test_run_once_suppresses_prom_dependent_when_prometheus_down(monkeypatch, cfg):
    ran, pushes = wire_run_once(cfg, monkeypatch, (False, "prom is down"))
    # the prom-dependent check is suppressed: never executed, pushed `up` with a skip msg
    assert "disk" not in ran
    assert "backup" in ran  # non-prom check still runs
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert by_tok["tok_disk"][0] is True
    assert "skipped" in by_tok["tok_disk"][1].lower()
    # the Prometheus monitor itself pushed down with its message
    assert any(ok is False and "prom is down" in msg for _, ok, msg in pushes)


def test_run_once_unreachable_prometheus_exception_suppresses(monkeypatch, cfg):
    # prom_scalar raising (the real outage path) -> _evaluate renders it down -> suppression
    ran, pushes = wire_run_once(cfg, monkeypatch, RuntimeError("connection refused"))
    assert "disk" not in ran
    assert "backup" in ran
    assert any(ok is False and "connection refused" in msg for _, ok, msg in pushes)


def test_run_once_runs_all_when_prometheus_up(monkeypatch, cfg):
    ran, pushes = wire_run_once(cfg, monkeypatch, (True, "ok"))
    assert ran == ["disk", "backup"]  # nothing suppressed
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert "skipped" not in by_tok["tok_disk"][1].lower()


# ── Loki reachability gate (peer of the Prometheus gate) ─────────────────────


def test_run_once_suppresses_loki_dependent_when_loki_down(monkeypatch, cfg):
    ran, pushes = wire_run_once_loki(
        cfg,
        monkeypatch,
        (False, "loki unreachable"),
        ["recyclarr", "janitorr", "backup"],
        {"recyclarr", "janitorr"},
    )
    # Loki-dependent checks suppressed (never run, pushed up w/ a skip msg); non-loki still runs
    assert not ({"recyclarr", "janitorr"} & set(ran))
    assert "backup" in ran
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_recyclarr"][0] is True
    assert "loki" in by_tok["tok_recyclarr"][1].lower()
    # the Loki Reachable monitor itself pushed down with its message
    assert any(ok is False and "loki unreachable" in m for _, ok, m in pushes)


def test_run_once_unreachable_loki_exception_suppresses(monkeypatch, cfg):
    # the Loki probe raising (the real outage path) -> _evaluate down -> suppression
    ran, _ = wire_run_once_loki(
        cfg,
        monkeypatch,
        RuntimeError("connection refused"),
        ["recyclarr", "backup"],
        {"recyclarr"},
    )
    assert "recyclarr" not in ran
    assert "backup" in ran


def test_run_once_runs_loki_dependent_when_loki_up(monkeypatch, cfg):
    ran, _ = wire_run_once_loki(
        cfg,
        monkeypatch,
        (True, "Loki reachable"),
        ["recyclarr", "janitorr"],
        {"recyclarr", "janitorr"},
    )
    assert "recyclarr" in ran and "janitorr" in ran


# ── Cluster-Prometheus gate (peer of the Prometheus gate, other instance) ────


def test_cluster_prometheus_gate_down_when_no_result(monkeypatch, cfg):
    cfg = replace(cfg, CLUSTER_PROM_URL="https://prom-k8s.example")
    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, *a, **k: None)
    ok, msg = checks.cluster.check_cluster_prometheus(cfg)
    assert ok is False
    assert "no result" in msg


def test_run_once_suppresses_cluster_dependent_when_cluster_prometheus_down(
    monkeypatch,
    cfg,
):
    # A cluster-side outage must page ONCE, as Cluster Prometheus — not as a workload fault.
    pushed = run_once_with_gates(
        cfg,
        monkeypatch,
        cluster_ok=False,
        checks=[("k8s_workloads", "tok", lambda _cfg: (False, "should not run"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is True
    assert "cluster Prometheus unreachable" in pushed["tok"][1]


def test_run_once_runs_cluster_dependent_when_cluster_prometheus_up(monkeypatch, cfg):
    pushed = run_once_with_gates(
        cfg,
        monkeypatch,
        cluster_ok=True,
        checks=[("k8s_workloads", "tok", lambda _cfg: (False, "real failure"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is False
    assert pushed["tok"][1] == "real failure"


def test_run_once_reads_every_gates_field(monkeypatch, cfg):
    """The seam must not be inert: a value passed on `Gates` has to reach the loop.

    Eleven fields, and a field `run_once` never reads is a knob a test can turn with no effect —
    a stated configuration and a green assertion agreeing about nothing. Two cycles, because the
    exporter probe runs only when the Prometheus gate is UP, so one cycle cannot exercise both
    `prom_dependent` and `exporter_dependent`. Every field is set to a sentinel no production
    table contains, so a field that stops being read fails here rather than quietly.
    """
    names = ["prom_dep", "exp_dep", "loki_dep", "b2_dep", "cluster_dep", "graced"]

    def body(name, seen):
        def fn(_cfg):
            seen.append(name)
            return False, "%s failed" % name

        return fn

    def cycle(prom_result, up_vector, streaks):
        seen, pushed = [], {}
        monkeypatch.setattr(
            bridge.net, "push", lambda _cfg, t, ok, m: pushed.setdefault(t, (ok, m))
        )
        monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, *a, **k: up_vector)
        check.run_once(
            cfg,
            [Check(n, "tok_%s" % n, body(n, seen)) for n in names],
            gates=Gates(
                prom_dependent=frozenset({"prom_dep"}),
                exporter_dependent={"sentinel_job": frozenset({"exp_dep"})},
                loki_dependent=frozenset({"loki_dep"}),
                b2_dependent=frozenset({"b2_dep"}),
                cluster_dependent=frozenset({"cluster_dep"}),
                startup_grace=frozenset({"graced"}),
                grace_streaks=streaks,
                probe_prometheus=lambda _cfg: prom_result,
                probe_loki=lambda _cfg: (False, "loki down"),
                probe_b2=lambda _cfg: (False, "b2 down"),
                probe_cluster=lambda _cfg: (False, "cluster down"),
            ),
        )
        return seen, pushed

    # Cycle 1 — Prometheus UP with the sentinel exporter job down. exporter_dependent,
    # loki_dependent, b2_dependent, cluster_dependent, startup_grace, grace_streaks and three of
    # the four probes all decide here.
    streaks = {}
    seen, pushed = cycle((True, "prom up"), [({"job": "sentinel_job"}, 0.0)], streaks)
    assert seen == ["prom_dep", "graced"]
    for name in ("exp_dep", "loki_dep", "b2_dep", "cluster_dep"):
        assert pushed["tok_%s" % name][0] is True, name
        assert "skipped" in pushed["tok_%s" % name][1], name
    # startup_grace + grace_streaks: `graced` went down but was held `up`, and the streak landed
    # in the dict that was PASSED rather than in bridge.streaks' module-level one.
    assert pushed["tok_graced"][0] is True
    assert "startup/redeploy grace" in pushed["tok_graced"][1]
    assert streaks == {"graced": 1}

    # Cycle 2 — Prometheus DOWN. prom_dependent and probe_prometheus decide here; the exporter
    # probe is deliberately not reached, so an empty `up` vector proves exp_dep ran on its own.
    seen, pushed = cycle((False, "prom down"), [], {})
    assert "prom_dep" not in seen
    assert "exp_dep" in seen
    assert pushed["tok_prom_dep"][0] is True
    assert "Prometheus unreachable" in pushed["tok_prom_dep"][1]


def test_duration_seconds_parses_prometheus_durations():
    assert bridge.parsing.duration_seconds("15m") == 900
    assert bridge.parsing.duration_seconds("1h") == 3600
    assert bridge.parsing.duration_seconds("90s") == 90
    assert bridge.parsing.duration_seconds("1d") == 86400
    for bad in ("", "15", "m", "1y", "abc"):
        with pytest.raises(ValueError):
            bridge.parsing.duration_seconds(bad)


# --- the gate configuration is required, not defaulted ---------------------------------------


def test_run_once_requires_a_gates_value(cfg):
    """The red-proof half: `run_once` used to read `gates = Gates() if gates is None else gates`.

    Under that default this call ran a full cycle against a SECOND production `Gates()` — its
    own `grace_streaks` binding aside, cli.main() already builds the one instance the pod uses,
    so a second one is a silent divergence rather than an error.
    """
    with pytest.raises(TypeError):
        check.run_once(cfg, [])  # ty: ignore[missing-argument]
