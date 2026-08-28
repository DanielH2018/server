"""The run-loop gates: what suppresses a check rather than what the check decides.

A monitor that cannot reach Prometheus, Loki or B2 must report "cannot tell", never "down" —
otherwise one unreachable dependency fires every monitor that reads it at once. The startup
grace and the down-streak hysteresis are the same idea over time: a check that has just come
up, or has been down once, is not yet evidence.

These are the tests that drive `run_once()` end to end with the transport stubbed, so they are
the ones that fail when the wiring changes rather than the logic.
"""

import importlib
import json
import os
import re
import time
from pathlib import Path

import pytest
import yaml

import bridge_common
import bridge_parsing
import check


def test_loki_reachable_ok(monkeypatch):
    monkeypatch.setattr(
        check, "_get_json", lambda *a, **k: {"status": "success", "data": ["job"]}
    )
    assert check.loki_reachable() is True
    ok, msg = check.check_loki_reachable()
    assert ok
    assert "reachable" in msg.lower()


def test_loki_reachable_non_success_raises(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        check.loki_reachable()


# ── Prometheus reachability gate + alert-storm suppression (L1) ──────────────


def test_check_prometheus_reachable(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: 1.0)
    ok, msg = check.check_prometheus()
    assert ok
    assert "reachable" in msg.lower()


def test_check_prometheus_no_data_is_down(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", lambda q: None)
    ok, msg = check.check_prometheus()
    assert not ok


def _wire_run_once(monkeypatch, prom_result):
    """Drive run_once with a tiny CHECKS list (one prom-dependent, one not) and capture pushes.

    Returns (ran, pushes): `ran` is the names of checks actually executed, `pushes` is
    [(token, ok, msg), ...] in push order (incl. the leading `prometheus` push).
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        check, "push", lambda token, ok, msg: pushes.append((token, ok, msg))
    )
    if isinstance(prom_result, Exception):

        def _prom():
            raise prom_result
    else:

        def _prom():
            return prom_result

    monkeypatch.setattr(check, "check_prometheus", _prom)
    # No exporters down by default, so the prom-up path doesn't hit the network probing `up`.
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset({"disk"}))
    # Loki reachable by default so run_once's Loki gate doesn't make a real network call here.
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(
        check,
        "CHECKS",
        [("disk", "tok_disk", _mk("disk")), ("backup", "tok_backup", _mk("backup"))],
    )
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_prom_dependent_when_prometheus_down(monkeypatch):
    ran, pushes = _wire_run_once(monkeypatch, (False, "prom is down"))
    # the prom-dependent check is suppressed: never executed, pushed `up` with a skip msg
    assert "disk" not in ran
    assert "backup" in ran  # non-prom check still runs
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert by_tok["tok_disk"][0] is True
    assert "skipped" in by_tok["tok_disk"][1].lower()
    # the Prometheus monitor itself pushed down with its message
    assert any(ok is False and "prom is down" in msg for _, ok, msg in pushes)


def test_run_once_unreachable_prometheus_exception_suppresses(monkeypatch):
    # prom_scalar raising (the real outage path) -> _evaluate renders it down -> suppression
    ran, pushes = _wire_run_once(monkeypatch, RuntimeError("connection refused"))
    assert "disk" not in ran
    assert "backup" in ran
    assert any(ok is False and "connection refused" in msg for _, ok, msg in pushes)


def test_run_once_runs_all_when_prometheus_up(monkeypatch):
    ran, pushes = _wire_run_once(monkeypatch, (True, "ok"))
    assert ran == ["disk", "backup"]  # nothing suppressed
    by_tok = {tok: (ok, msg) for tok, ok, msg in pushes}
    assert "skipped" not in by_tok["tok_disk"][1].lower()


def test_prom_dependent_set_matches_real_checks():
    # Guard: every name in PROM_DEPENDENT is a real check, so the gate can't silently drift.
    names = {name for name, _, _ in check.CHECKS}
    assert check.PROM_DEPENDENT <= names


# ── Loki reachability gate (peer of the Prometheus gate) ─────────────────────


def test_loki_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every name in LOKI_DEPENDENT is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.LOKI_DEPENDENT <= names


def _wire_run_once_loki(monkeypatch, loki_result, checks, loki_dependent):
    """Drive run_once with Prometheus UP and a stubbed Loki-reachability result; capture run+push."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset(loki_dependent))
    if isinstance(loki_result, Exception):

        def _loki():
            raise loki_result
    else:

        def _loki():
            return loki_result

    monkeypatch.setattr(check, "check_loki_reachable", _loki)

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_loki_dependent_when_loki_down(monkeypatch):
    ran, pushes = _wire_run_once_loki(
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


def test_run_once_unreachable_loki_exception_suppresses(monkeypatch):
    # check_loki_reachable raising (the real outage path) -> _evaluate down -> suppression
    ran, _ = _wire_run_once_loki(
        monkeypatch,
        RuntimeError("connection refused"),
        ["recyclarr", "backup"],
        {"recyclarr"},
    )
    assert "recyclarr" not in ran
    assert "backup" in ran


def test_run_once_runs_loki_dependent_when_loki_up(monkeypatch):
    ran, _ = _wire_run_once_loki(
        monkeypatch,
        (True, "Loki reachable"),
        ["recyclarr", "janitorr"],
        {"recyclarr", "janitorr"},
    )
    assert "recyclarr" in ran and "janitorr" in ran


# ── B2 reachability gate (peer of the Prometheus/Loki gates) ─────────────────


def test_b2_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every name in B2_DEPENDENT is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.B2_DEPENDENT <= names


def test_b2_dependent_excludes_backup():
    # `backup` polls Kopia live and correctly paged through the 2026-08-02 cap breach — it is the
    # one true signal, so the gate must not suppress it. It is also in STARTUP_GRACE, which has to
    # stay disjoint from every skip set (see test_startup_grace_disjoint_from_run_once_skip_sets).
    assert "backup" not in check.B2_DEPENDENT


def test_cluster_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT/B2_DEPENDENT): every name is a real check.
    names = {name for name, _, _ in check.CHECKS}
    assert check.CLUSTER_DEPENDENT <= names


def test_cluster_dependent_disjoint_from_prom_dependent():
    # The whole point of a second gate: k8s_workloads reads the CLUSTER Prometheus, so it must not
    # also be suppressed by the DOCKER Prometheus gate. Being in both would mean a Docker-side
    # outage silences a check whose source is fine, and vice versa.
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.PROM_DEPENDENT)
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.LOKI_DEPENDENT)
    assert check.CLUSTER_DEPENDENT.isdisjoint(check.B2_DEPENDENT)


def test_k8s_workloads_absent_series_is_down_not_up():
    # THE regression this check exists to prevent. `unavailable > 0` returns an empty vector both
    # when everything is healthy and when there are no series at all; reading the healthy meaning
    # onto both is how a monitor goes green while blind.
    ok, msg = check.k8s_workloads_verdict(None, [], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_workloads_partial_series_is_down():
    # A partially-loaded kube-state-metrics — e.g. `apps` dropped from its scoped ClusterRole,
    # which takes every deployment series away while the pod stays up and Ready.
    ok, msg = check.k8s_workloads_verdict(2, [], 5)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_workloads_healthy_when_series_present_and_none_unavailable():
    ok, msg = check.k8s_workloads_verdict(18, [], 5)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_k8s_workloads_names_the_offenders():
    offenders = [
        ({"deployment": "n8n-runners"}, 1.0),
        ({"deployment": "registry"}, 2.0),
    ]
    ok, msg = check.k8s_workloads_verdict(18, offenders, 5)
    assert ok is False
    # Sorted, so the message is stable rather than dependent on Prometheus' series order.
    assert "n8n-runners(1), registry(2)" in msg


def test_k8s_workloads_crash_loop_is_down_despite_available_replicas():
    # The 2026-08-13 homepage incident: a CrashLoopBackOff pod passes readiness for a brief
    # window each backoff cycle, so replica availability read healthy through 31 restarts.
    # The restart counter is the signal that doesn't flap.
    restarts = [({"pod": "homepage-58d867556f-7qbz9"}, 6.0)]
    ok, msg = check.k8s_workloads_verdict(18, [], 5, restarts)
    assert ok is False
    assert "crash-looping" in msg
    assert "homepage-58d867556f-7qbz9(6)" in msg


def test_k8s_workloads_unavailable_replicas_outrank_the_restart_arm():
    # Both arms firing is one incident; the replica message is the more actionable one.
    offenders = [({"deployment": "homepage"}, 1.0)]
    restarts = [({"pod": "homepage-x"}, 6.0)]
    ok, msg = check.k8s_workloads_verdict(18, offenders, 5, restarts)
    assert ok is False
    assert "unavailable replicas" in msg


def test_k8s_daemonsets_absent_series_is_down_not_up():
    # Same fail-closed shape as the deployment arm: an absent DaemonSet series is UNKNOWN,
    # not "no DaemonSets have a problem".
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=None, min_daemonsets=9)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_daemonsets_partial_series_is_down():
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=3, min_daemonsets=9)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_daemonsets_names_the_offenders():
    ds_offenders = [({"daemonset": "otel-collector"}, 1.0)]
    ok, msg = check.k8s_workloads_verdict(
        18, [], 5, ds_total=9, ds_offenders=ds_offenders, min_daemonsets=9
    )
    assert ok is False
    assert "otel-collector(1)" in msg


def test_k8s_daemonsets_healthy_alongside_healthy_deployments():
    ok, msg = check.k8s_workloads_verdict(18, [], 5, ds_total=9, min_daemonsets=9)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_origin_sel_is_empty_without_a_pin(monkeypatch):
    # Against the Docker Prometheus there is no `origin` label at all — external_labels apply on
    # remote-write and never to local storage — so a pin there would select NOTHING and read as
    # healthy. Empty must stay empty.
    monkeypatch.setattr(check, "PROM_ORIGIN", "")
    assert check.origin_sel() == ""
    assert check.origin_sel('name!=""') == '{name!=""}'


def test_origin_sel_appends_the_pin(monkeypatch):
    monkeypatch.setattr(check, "PROM_ORIGIN", 'origin="daniel-server"')
    assert check.origin_sel() == '{origin="daniel-server"}'
    assert check.origin_sel('name!=""') == '{name!="", origin="daniel-server"}'


def test_origin_pin_derives_from_the_prometheus_url(monkeypatch):
    # THE regression this guards. PROM_ORIGIN is derived rather than configured precisely so it
    # cannot drift out of lockstep with PROMETHEUS_URL: pointing one at the cluster and forgetting
    # the other selects nothing, which every one of these checks decodes as healthy.
    monkeypatch.setenv("PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(check)
    try:
        assert reloaded.PROM_ORIGIN == 'origin="daniel-server"'
    finally:
        monkeypatch.undo()
        importlib.reload(check)


def test_origin_pin_absent_when_reading_the_docker_prometheus(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(check)
    try:
        assert reloaded.PROM_ORIGIN == ""
    finally:
        monkeypatch.undo()
        importlib.reload(check)


def test_cluster_targets_is_cluster_dependent_not_prom_dependent():
    # It reads the CLUSTER Prometheus, so a Docker-side outage must not suppress it and vice
    # versa — the same separation k8s_workloads has.
    assert "cluster_targets" in check.CLUSTER_DEPENDENT
    assert "cluster_targets" not in check.PROM_DEPENDENT


def test_cluster_targets_covers_everything_its_sibling_does_not(monkeypatch):
    """`origin!="daniel-server"` is the complement of check_targets_down's pin, so every `up`
    series belongs to exactly one of the two checks.

    THE GAP THIS PINS (2026-08-15): the previous `origin=""` matched only series where the label
    is ABSENT (cluster-native). daniel-box's node-exporter carries `origin="daniel-box"`, so it
    matched NEITHER check and could have died watched by nothing.
    """
    seen = {}

    def fake_vector(promql, base=None, source="prometheus"):
        seen["q"], seen["base"] = promql, base
        return [({"job": "j%d" % i}, 1.0) for i in range(5)]

    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "https://cluster")
    monkeypatch.setattr(check, "prom_vector", fake_vector)
    ok, _ = check.check_cluster_targets()
    assert ok is True
    assert seen["q"] == 'up{origin!="daniel-server"}'
    assert seen["base"] == "https://cluster"


def test_cluster_targets_empty_is_down():
    ok, msg = check.targets_verdict([], check.CLUSTER_TARGETS_MIN)
    assert ok is False
    assert "UNKNOWN" in msg


def test_cluster_targets_disabled_without_cluster_url(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "")
    ok, msg = check.check_cluster_targets()
    assert ok is True
    assert "disabled" in msg


def test_targets_empty_vector_is_down_not_all_clear():
    # THE hole B5 opens. Before the repoint an empty `up` could only mean the queried Prometheus
    # was down, and the PROM_DEPENDENT gate suppressed this check first. Against the cluster copy
    # the gate passes (that Prometheus is fine) while `up{origin="daniel-server"}` is empty, and
    # the old code returned "all 0 targets up".
    ok, msg = check.targets_verdict([], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_targets_below_floor_is_down():
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is False
    assert "below the floor" in msg


def test_targets_names_down_jobs_above_the_floor():
    vec = [({"job": "node"}, 0.0)] + [({"job": "j%d" % i}, 1.0) for i in range(5)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is False
    assert "1 target(s) down: node" in msg


def test_targets_all_up_above_the_floor():
    vec = [({"job": "j%d" % i}, 1.0) for i in range(11)]
    ok, msg = check.targets_verdict(vec, 5)
    assert ok is True
    assert msg == "all 11 targets up"


_CADVISOR_METRICS = (
    "container_start_time_seconds",
    "container_oom_events_total",
    "container_cpu_cfs_throttled_periods_total",
    "container_cpu_cfs_periods_total",
    "container_cpu_cfs_throttled_seconds_total",
)


def test_cadvisor_checks_never_pin_the_origin():
    # REPLACES test_dual_estate_checks_all_pin_the_origin, which asserted the exact opposite and
    # was wrong from the Phase G retarget until 2026-08-24. Its premise — that these metrics
    # "genuinely exist in BOTH estates" — died with the Docker cAdvisor on 2026-08-14. `origin` is
    # set by ONE relabel rule, on the `node` job, so cAdvisor series never carry it; pinning them
    # selects the empty vector and check_restarts/check_oom/check_cpu report green forever.
    #
    # The old test enforced that bug rather than catching it, which is why the fix had to amend a
    # green test rather than a red one. Keep this assertion pointed at the SOURCE of the pin.
    source = Path(check.__file__).read_text()
    for metric in _CADVISOR_METRICS:
        assert metric + "{" not in source, (
            "%s uses a literal label block; route it through cadvisor_sel() so the matcher set "
            "stays in one place" % metric
        )
    # The real regression guard: no cAdvisor query may be built with origin_sel(). Checked by
    # rendering both helpers and asserting the origin pin reaches only the one that should carry
    # it — a textual check on the call site would miss a pin applied via an intermediate variable,
    # which is exactly the shape check_cpu uses (`sel = ...` then two format calls).
    # Guarded on PROM_ORIGIN being non-empty: under the test env PROM_URL is unset so the pin is
    # "", and `"" not in s` is False for every s — an unguarded assert fails on the empty case
    # while proving nothing about the real one.
    if check.PROM_ORIGIN:
        assert check.PROM_ORIGIN not in check.cadvisor_sel('container!=""'), (
            "cadvisor_sel() must not apply the origin pin — cAdvisor series carry no origin label"
        )
    # Independent of the environment: the pin can only enter through PROM_ORIGIN, so a
    # cadvisor_sel() built with a sentinel pin must still come back without it.
    saved = check.PROM_ORIGIN
    try:
        check.PROM_ORIGIN = 'origin="sentinel"'
        assert "sentinel" not in check.cadvisor_sel('container!=""')
        assert "sentinel" in check.origin_sel('container!=""')
    finally:
        check.PROM_ORIGIN = saved


def test_up_still_pins_the_origin_where_the_label_exists():
    # The other half of the contract: `up` DOES carry origin (the node job is relabelled), and
    # targets_verdict depends on the pin to scope its floor to one estate. Dropping it there would
    # make check_targets a duplicate of check_cluster_targets and orphan daniel-server's
    # node-exporter, which is why the fix deliberately left this call site alone.
    if check.PROM_ORIGIN:
        assert check.PROM_ORIGIN in check.origin_sel(), (
            "origin_sel() must still apply the pin when PROM_ORIGIN is set"
        )


def test_duration_seconds_parses_prometheus_durations():
    assert bridge_parsing.duration_seconds("15m") == 900
    assert bridge_parsing.duration_seconds("1h") == 3600
    assert bridge_parsing.duration_seconds("90s") == 90
    assert bridge_parsing.duration_seconds("1d") == 86400
    for bad in ("", "15", "m", "1y", "abc"):
        with pytest.raises(ValueError):
            bridge_parsing.duration_seconds(bad)


def test_k8s_workloads_disabled_without_cluster_url(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "")
    ok, msg = check.check_k8s_workloads()
    assert ok is True
    assert "disabled" in msg


def test_cluster_prometheus_gate_down_when_no_result(monkeypatch):
    monkeypatch.setattr(check, "CLUSTER_PROM_URL", "https://prom-k8s.example")
    monkeypatch.setattr(check, "prom_scalar", lambda *a, **k: None)
    ok, msg = check.check_cluster_prometheus()
    assert ok is False
    assert "no result" in msg


def test_run_once_suppresses_cluster_dependent_when_cluster_prometheus_down(
    monkeypatch,
):
    # A cluster-side outage must page ONCE, as Cluster Prometheus — not as a workload fault.
    pushed = _run_once_with_gates(
        monkeypatch,
        cluster_ok=False,
        checks=[("k8s_workloads", "tok", lambda: (False, "should not run"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is True
    assert "cluster Prometheus unreachable" in pushed["tok"][1]


def test_run_once_runs_cluster_dependent_when_cluster_prometheus_up(monkeypatch):
    pushed = _run_once_with_gates(
        monkeypatch,
        cluster_ok=True,
        checks=[("k8s_workloads", "tok", lambda: (False, "real failure"))],
        cluster_dependent={"k8s_workloads"},
    )
    assert pushed["tok"][0] is False
    assert pushed["tok"][1] == "real failure"


def _run_once_with_gates(monkeypatch, cluster_ok, checks, cluster_dependent):
    """Drive run_once with every gate but the cluster one forced healthy."""
    pushed = {}
    monkeypatch.setattr(check, "CHECKS", checks)
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset())
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "B2_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "CLUSTER_DEPENDENT", frozenset(cluster_dependent))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_b2_reachable", lambda: (True, "up"))
    monkeypatch.setattr(check, "check_cluster_prometheus", lambda: (cluster_ok, "gate"))
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    monkeypatch.setattr(
        check, "push", lambda token, ok, msg: pushed.__setitem__(token, (ok, msg))
    )
    monkeypatch.setattr(bridge_common, "log", lambda *a, **k: None)
    check.run_once()
    return pushed


def _reset_b2_probe(monkeypatch, key_id="kid", app_key="akey", interval=1800):
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", key_id)
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", app_key)
    monkeypatch.setattr(check, "B2_PROBE_INTERVAL_S", interval)
    monkeypatch.setattr(
        check, "_b2_probe", {"ts": 0.0, "ok": True, "msg": "not yet probed"}
    )


def test_b2_reachable_disabled_without_credentials(monkeypatch):
    _reset_b2_probe(monkeypatch, key_id="", app_key="")
    ok, msg = check.b2_reachable(now=10_000)
    assert ok is True and "disabled" in msg


@pytest.mark.parametrize(
    ("response", "ok", "must_contain"),
    [
        pytest.param({"accountId": "a1"}, True, ("reachable",), id="ok_on_account_id"),
        # Version-tolerant: Backblaze publishes a v4 body example (accountId top-level) but none
        # for v3, so either field proves it's B2. Pinning one shape would page every cycle if it
        # moved.
        pytest.param(
            {"authorizationToken": "t"},
            True,
            (),
            id="accepts_authorization_token_only",
        ),
        # A 200 from something that isn't B2 must not read as healthy.
        pytest.param(
            {"unexpected": 1}, False, ("accountId",), id="rejects_unrecognised_response"
        ),
    ],
)
def test_b2_authorize(monkeypatch, response, ok, must_contain):
    monkeypatch.setattr(check, "B2_PROBE_KEY_ID", "kid")
    monkeypatch.setattr(check, "B2_PROBE_APPLICATION_KEY", "akey")
    monkeypatch.setattr(check, "_get_json", lambda url, headers=None: response)
    result_ok, msg = check.b2_authorize()
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_b2_reachable_surfaces_the_cap_error_text(monkeypatch):
    # G3: the alert must name the CAUSE. B2 answers a cap breach with transaction_cap_exceeded,
    # and _get_json appends the response body to the HTTPError, so it has to reach the message.
    _reset_b2_probe(monkeypatch)

    def _boom(url, headers=None):
        raise RuntimeError("HTTP Error 403: transaction_cap_exceeded")

    monkeypatch.setattr(check, "_get_json", _boom)
    ok, msg = check.b2_reachable(now=10_000)
    assert ok is False and "transaction_cap_exceeded" in msg


def test_b2_reachable_caches_failure_and_does_not_reprobe(monkeypatch):
    # THE cost-critical property. The fault being detected is a transaction cap, so a failure must
    # NOT re-probe every cycle the way email_backstop does — that would spend the exhausted budget.
    _reset_b2_probe(monkeypatch)
    calls = []

    def _boom(url, headers=None):
        calls.append(url)
        raise RuntimeError("HTTP Error 403: transaction_cap_exceeded")

    monkeypatch.setattr(check, "_get_json", _boom)
    first_ok, _ = check.b2_reachable(now=10_000)
    # five more cycles inside the interval (INTERVAL=300 -> 25 min of cycles)
    for offset in (300, 600, 900, 1200, 1500):
        ok, msg = check.b2_reachable(now=10_000 + offset)
        assert ok is False
        assert (
            "transaction_cap_exceeded" in msg
        )  # cached verdict still reported every cycle
    assert first_ok is False
    assert len(calls) == 1, "a cached failure must not re-probe: %d calls" % len(calls)


def test_b2_reachable_reprobes_after_the_interval(monkeypatch):
    _reset_b2_probe(monkeypatch, interval=1800)
    calls = []

    def _ok(url, headers=None):
        calls.append(url)
        return {"accountId": "a1"}

    monkeypatch.setattr(check, "_get_json", _ok)
    check.b2_reachable(now=10_000)
    check.b2_reachable(now=10_000 + 1799)  # still cached
    assert len(calls) == 1
    check.b2_reachable(now=10_000 + 1801)  # interval elapsed
    assert len(calls) == 2


def _wire_run_once_b2(monkeypatch, b2_result, checks, b2_dependent):
    """Drive run_once with Prometheus+Loki UP and a stubbed B2-reachability result."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset())
    monkeypatch.setattr(check, "B2_DEPENDENT", frozenset(b2_dependent))
    monkeypatch.setattr(check, "check_b2_reachable", lambda: b2_result)

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_b2_dependent_when_b2_down(monkeypatch):
    ran, pushes = _wire_run_once_b2(
        monkeypatch,
        (False, "B2 unreachable: HTTP Error 403: transaction_cap_exceeded"),
        ["b2_usage", "verify", "backup"],
        {"b2_usage", "verify"},
    )
    # The four state-file checks stop reporting their last-successful-run as current health...
    assert not ({"b2_usage", "verify"} & set(ran))
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_b2_usage"][0] is True
    assert "b2" in by_tok["tok_b2_usage"][1].lower()
    # ...while Backup Freshness still runs and can page — it is the signal that was right.
    assert "backup" in ran
    assert any(
        ok is False and "transaction_cap_exceeded" in m for _, ok, m in pushes
    ), "the B2 Reachable monitor must page with B2's own error text"


def test_run_once_runs_b2_dependent_when_b2_up(monkeypatch):
    ran, _ = _wire_run_once_b2(
        monkeypatch,
        (True, "B2 reachable"),
        ["b2_usage", "verify"],
        {"b2_usage", "verify"},
    )
    assert "b2_usage" in ran and "verify" in ran


# ── Exporter-reachability gate (node-exporter / cadvisor) — Backups M3 ───────


@pytest.mark.parametrize(
    ("up", "expected"),
    [
        pytest.param(
            [
                ({"job": "node"}, 0.0),
                ({"job": "cadvisor"}, 1.0),
                ({"job": "prometheus"}, 1.0),
            ],
            {"node"},
            id="flags_node_when_node_up_is_zero",
        ),
        pytest.param(
            [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 0.0)],
            {"node"},
            # cadvisor left EXPORTER_DEPENDENT when it retired (2026-08-14) — a down series under
            # its old job name must no longer trigger suppression of anything.
            id="flags_only_mapped_exporters",
        ),
        pytest.param(
            [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)],
            set(),
            id="empty_when_all_up",
        ),
        pytest.param(
            [
                ({"job": "loki"}, 0.0),
                ({"job": "node"}, 1.0),
                ({"job": "cadvisor"}, 1.0),
            ],
            set(),
            # A non-exporter target down (e.g. loki) is Scrape Targets' concern, not a
            # suppression trigger.
            id="ignores_non_exporter_jobs",
        ),
    ],
)
def test_down_exporters(up, expected):
    assert check.down_exporters(up) == expected


def test_exporter_dependent_values_are_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every suppressed dependent is a real check name, so the
    # exporter gate can't silently drift, and every dependent is also prom-dependent.
    names = {name for name, _, _ in check.CHECKS}
    for deps in check.EXPORTER_DEPENDENT.values():
        assert deps <= names
        assert deps <= check.PROM_DEPENDENT


def _wire_run_once_prom_up(monkeypatch, up_vector, checks, prom_dependent):
    """Drive run_once with Prometheus UP and a stubbed `up` vector; capture what ran + pushed."""
    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", lambda q: up_vector if q == "up" else [])
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset(prom_dependent))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [(n, "tok_%s" % n, _mk(n)) for n in checks])
    check.run_once()
    return ran, pushes


def test_run_once_suppresses_node_dependents_when_node_exporter_down(monkeypatch):
    up = [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 1.0)]
    ran, pushes = _wire_run_once_prom_up(
        monkeypatch,
        up,
        ["disk", "memory", "targets"],
        {"disk", "memory", "targets"},
    )
    # node-dependents suppressed (never run, pushed up with a skip msg); Scrape Targets still pages
    assert not ({"disk", "memory"} & set(ran))
    assert "targets" in ran
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_disk"][0] is True
    assert "exporter" in by_tok["tok_disk"][1].lower()


def _fake_vectors(monkeypatch, by_query):
    """prom_vector stub keyed by substring of the query.

    Drops CADVISOR_PODS_MIN to 0 for its callers, which are all offender-logic tests built on
    one- or two-pod fixtures — far below the real floor. Scoped here rather than as an autouse
    fixture on purpose: an estate-wide default of 0 would make the coverage floor invisible to
    every other test in the suite, which is the failure the floor itself exists to prevent. The
    floor's own tests stub prom_vector directly and never come through here.
    """
    monkeypatch.setattr(check, "CADVISOR_PODS_MIN", 0)

    def fake(promql):
        for key, vec in by_query.items():
            if key in promql:
                return vec
        raise AssertionError("unexpected query: %s" % promql)

    monkeypatch.setattr(check, "prom_vector", fake)


def test_check_restarts_names_the_looping_pod(monkeypatch):
    _fake_vectors(
        monkeypatch,
        {
            "container_start_time_seconds": [
                ({"pod": "n8n-abc"}, 7.0),
                ({"pod": "quiet"}, 0.0),
            ]
        },
    )
    ok, msg = check.check_restarts()
    assert not ok and "n8n-abc" in msg


def test_check_restarts_quiet_is_up(monkeypatch):
    _fake_vectors(
        monkeypatch, {"container_start_time_seconds": [({"pod": "quiet"}, 1.0)]}
    )
    ok, _ = check.check_restarts()
    assert ok


def test_check_oom_names_the_killed_pod(monkeypatch):
    _fake_vectors(
        monkeypatch, {"container_oom_events_total": [({"pod": "karakeep-x"}, 2.0)]}
    )
    ok, msg = check.check_oom()
    assert not ok and "karakeep-x" in msg


def test_check_cpu_throttle_needs_both_gates_and_streak(monkeypatch):
    # 90% throttled AND real cores lost — but only pages on the CPU_CONSECUTIVE-th
    # consecutive breaching cycle.
    check._cpu_breach_streak = 0
    _fake_vectors(
        monkeypatch,
        {
            "container_cpu_cfs_throttled_periods_total": [({"pod": "tdarr-y"}, 0.9)],
            "container_cpu_cfs_throttled_seconds_total": [({"pod": "tdarr-y"}, 0.5)],
        },
    )
    for _ in range(check.CPU_CONSECUTIVE - 1):
        ok, msg = check.check_cpu_throttle()
        assert ok and "tdarr-y" in msg  # named but not paging yet
    ok, msg = check.check_cpu_throttle()
    assert not ok and "tdarr-y" in msg
    check._cpu_breach_streak = 0


def test_check_cpu_throttle_tiny_loss_stays_up(monkeypatch):
    # High ratio but negligible absolute cores lost — the volume floor gates it out.
    check._cpu_breach_streak = 0
    _fake_vectors(
        monkeypatch,
        {
            "container_cpu_cfs_throttled_periods_total": [({"pod": "sidecar"}, 0.9)],
            "container_cpu_cfs_throttled_seconds_total": [({"pod": "sidecar"}, 0.0001)],
        },
    )
    ok, _ = check.check_cpu_throttle()
    assert ok


def test_run_once_suppression_without_cadvisor_series(monkeypatch):
    # Post-retirement shape: only the node job exists in `up`.
    up = [({"job": "node"}, 0.0)]
    ran, _ = _wire_run_once_prom_up(
        monkeypatch,
        up,
        ["disk", "memory", "targets"],
        {"disk", "memory", "targets"},
    )
    assert not ({"disk", "memory"} & set(ran))
    assert "targets" in ran


def test_run_once_no_suppression_when_exporters_up(monkeypatch):
    up = [({"job": "node"}, 1.0)]
    ran, _ = _wire_run_once_prom_up(
        monkeypatch, up, ["disk", "memory"], {"disk", "memory"}
    )
    assert "disk" in ran and "memory" in ran


def test_run_once_up_probe_failure_does_not_suppress(monkeypatch):
    # If the `up` probe itself errors, fail toward alerting: run the checks, don't mask them.
    def boom(q):
        raise RuntimeError("prom hiccup")

    ran, pushes = [], []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "prom_vector", boom)
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset({"disk"}))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [("disk", "tok_disk", _mk("disk"))])
    check.run_once()
    assert "disk" in ran  # not suppressed


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
    new_count, ok, msg = check.down_streak(
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
    monkeypatch.setattr(check, "prom_vector", lambda q: [])
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "PROM_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "LOKI_DEPENDENT", frozenset())
    monkeypatch.setattr(check, "STARTUP_GRACE", frozenset({"n8n"}))
    monkeypatch.setattr(check, "GRACE_CYCLES", 2)
    monkeypatch.setattr(check, "_grace_streaks", {})
    seq = iter(results)
    monkeypatch.setattr(check, "CHECKS", [("n8n", "tok_n8n", lambda: next(seq))])
    pushes = []
    monkeypatch.setattr(check, "push", lambda t, ok, m: pushes.append((t, ok, m)))
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

    monkeypatch.setattr(check, "prom_scalar", fake_scalar)
    ok, _ = check.check_promtail_dropped()
    assert not ok
    # No reason filter — sums drops across ALL reasons (rate_limited/stream_limited/... too, M2).
    assert any(
        "increase(" in q and "promtail_dropped_entries_total" in q and "reason" not in q
        for q in queries
    )


# --- cAdvisor coverage floor -------------------------------------------------------------
# restarts/oom/cpu filter a per-pod vector down to offenders, so empty-after-filtering is the
# HEALTHY answer and an empty query is indistinguishable from it. That split ran live from the
# Phase G retarget to 2026-08-24 with all three logging OK, found by reading the code rather than
# by an alert. Each pair below is one input the floor must accept and one it must reject.


def _reset_cadvisor(monkeypatch, min_pods=20, consecutive=2):
    monkeypatch.setattr(check, "CADVISOR_PODS_MIN", min_pods)
    monkeypatch.setattr(check, "CADVISOR_CONSECUTIVE", consecutive)
    monkeypatch.setattr(check, "_cadvisor_streaks", {})


def test_cadvisor_coverage_above_the_floor_is_clean():
    assert check.cadvisor_coverage_shortfall(20, 20, "OOM kills") is None


def test_cadvisor_coverage_below_the_floor_is_flagged():
    msg = check.cadvisor_coverage_shortfall(19, 20, "OOM kills")
    assert msg is not None
    assert "UNKNOWN" in msg
    assert "below the floor of 20" in msg


def test_cadvisor_empty_vector_is_flagged():
    # The 2026-08-24 shape exactly: an origin-pinned selector matched nothing.
    msg = check.cadvisor_coverage_shortfall(0, 20, "CPU throttling")
    assert msg is not None
    assert "matching nothing" in msg


def test_a_covered_vector_with_zero_offenders_still_reads_clean(monkeypatch):
    # The inversion this floor could most easily introduce: "no OOM kills" is the common case and
    # must stay green. Without this the floor would page on every healthy cycle.
    _reset_cadvisor(monkeypatch)
    monkeypatch.setattr(
        check,
        "prom_vector",
        lambda *a, **k: [({"pod": "p%d" % i}, 0.0) for i in range(40)],
    )
    ok, msg = check.check_oom()
    assert ok is True
    assert "no OOM kills" in msg


def test_check_oom_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, msg = check.check_oom()
    assert ok is False
    assert "UNKNOWN" in msg


def test_check_restarts_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, msg = check.check_restarts()
    assert ok is False
    assert "UNKNOWN" in msg


def test_check_cpu_throttle_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, msg = check.check_cpu_throttle()
    assert ok is False
    assert "UNKNOWN" in msg


def test_the_floor_holds_up_for_one_cycle_before_paging(monkeypatch):
    # A kubelet restart briefly empties cAdvisor; three monitors going red together on one
    # transient is the storm the gates exist to prevent.
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, msg = check.check_oom()
    assert ok is True
    assert "cAdvisor coverage shortfall 1/2" in msg
    ok, _ = check.check_oom()
    assert ok is False


def test_each_check_ages_its_shortfall_independently(monkeypatch):
    # A single shared counter would take three increments per cycle — all three checks run in the
    # same run_once pass — and blow through CADVISOR_CONSECUTIVE inside the first one.
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    assert check.check_oom()[0] is True
    assert check.check_restarts()[0] is True
    assert check.check_cpu_throttle()[0] is True
    assert check._cadvisor_streaks == {"oom": 1, "restarts": 1, "cpu": 1}


def test_a_covered_cycle_resets_the_streak(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    check.check_oom()
    monkeypatch.setattr(
        check,
        "prom_vector",
        lambda *a, **k: [({"pod": "p%d" % i}, 0.0) for i in range(40)],
    )
    assert check.check_oom()[0] is True
    assert check._cadvisor_streaks["oom"] == 0


def test_cadvisor_floor_is_overridable_from_the_env_secret():
    env_secret = (
        Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    )
    assert 'CADVISOR_PODS_MIN: "20"' in env_secret.read_text(), (
        "CADVISOR_PODS_MIN must be rendered in env-secret.yaml.j2 so an operator can tune it "
        "without a code change, like HOST_ORIGINS_MIN"
    )


def _functions_calling(name):
    """Every top-level function in check.py whose body calls `name`, by AST rather than by text.

    Derived, not enumerated. `_CADVISOR_METRICS` above is a literal tuple and the assertion it
    drives is about origin-pinning, not about the empty-vector floor — so before this, a FOURTH
    cAdvisor-derived check added later would inherit the pre-#495 "empty vector reads green"
    defect with every test still passing. That is the guard-scope class the estate has now carried
    five runs: a guard written alongside its fix inherits the fix's scope.
    """
    import ast

    tree = ast.parse(Path(check.__file__).read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == name:
                out.add(node.name)
    return out


def test_every_cadvisor_query_is_floored():
    """A cAdvisor check that skips the floor reads GREEN on an empty vector.

    cAdvisor series carry no `origin` label, so an outage or a relabel change empties the vector
    rather than erroring — and `all(...)` over nothing is True. #495 applied `_cadvisor_blind` to
    the three checks that existed; this derives the set instead, so a fourth fails here rather
    than shipping the old defect.
    """
    builders = _functions_calling("cadvisor_sel")
    floored = _functions_calling("_cadvisor_blind")
    assert builders, (
        "no function calls cadvisor_sel() — either the helper was renamed or this guard has "
        "stopped matching; a guard that matches nothing passes for the wrong reason"
    )
    missing = sorted(builders - floored)
    assert not missing, (
        "%s build a cAdvisor query without calling _cadvisor_blind(); an empty vector there "
        "reports green instead of UNKNOWN" % ", ".join(missing)
    )


def test_the_floor_helper_is_reached_by_every_check_that_needs_it():
    """The reject direction of the pair above: prove the derivation can actually find a gap.

    Asserting only `builders <= floored` would also pass if `_functions_calling` silently
    returned the empty set for both — the failure mode this repo calls a widening that lands
    green and inert. So pin the known membership too.
    """
    floored = _functions_calling("_cadvisor_blind")
    for expected in ("check_restarts", "check_oom", "check_cpu_throttle"):
        assert expected in floored, (
            "%s no longer calls _cadvisor_blind — #495's floor was removed from a check that "
            "had it" % expected
        )


# --- the etcd restore drill's stamp reader ------------------------------------------------------
#
# The reader was deliberately held back until the drill had a cron, because a fail-closed staleness
# check against a stamp nothing keeps fresh sits red forever and trains the operator to ignore it.
# The cron landed 2026-08-28 (k3s_etcd_restore_drill_cron), so these are the guards that came with
# the reader. Every one of them is a way the check could report GREEN while the restore path is
# unproven, which is the only failure mode that matters here.


def _stamp(tmp_path, monkeypatch, body, mode=0o644, name="last-success-list-only"):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(mode)
    monkeypatch.setattr(check, "ETCD_DRILL_STATE_DIR", str(tmp_path))
    return p


def _stamp_body(age_days, mode="list-only"):
    epoch = time.time() - age_days * 86400
    return "mode=%s\nsnapshot=x.zip\nutc=whenever\nepoch=%f\n" % (mode, epoch)


def test_etcd_drill_passes_on_a_recent_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, _stamp_body(1))
    ok, msg = check.check_etcd_restore_drill()
    assert ok is True
    assert "1.0 days ago" in msg


def test_etcd_drill_fails_when_it_has_never_run(tmp_path, monkeypatch):
    """The state most worth reporting, and the one `[[ -f $STAMP ]] && check_age` reports green."""
    monkeypatch.setattr(check, "ETCD_DRILL_STATE_DIR", str(tmp_path))
    ok, msg = check.check_etcd_restore_drill()
    assert ok is False
    assert "has ever passed" in msg


def test_etcd_drill_fails_when_the_stamp_is_unreadable(tmp_path, monkeypatch):
    """Not hypothetical: the first real run wrote 0640 root:root under UMASK 027 while this pod
    runs as uid 1000. An unreadable stamp and an absent one are otherwise indistinguishable, so
    they must report distinctly — they need different fixes."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits this asserts")
    _stamp(tmp_path, monkeypatch, _stamp_body(1), mode=0o000)
    ok, msg = check.check_etcd_restore_drill()
    assert ok is False
    assert "unreadable" in msg


def test_etcd_drill_fails_on_a_stale_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, _stamp_body(9))
    ok, msg = check.check_etcd_restore_drill()
    assert ok is False
    assert "9.0 days ago" in msg


def test_etcd_drill_fails_on_an_unparseable_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, "mode=list-only\nsnapshot=x.zip\n")
    ok, msg = check.check_etcd_restore_drill()
    assert ok is False
    assert "epoch" in msg


def test_etcd_drill_never_accepts_the_full_stamp_as_coverage(tmp_path, monkeypatch):
    """Only the list-only leg is scheduled. Accepting `last-success-full` would report the
    object-graph restore as proven when nothing on this host has ever proven it — the
    'one tier hiding behind another tier's evidence' shape."""
    _stamp(tmp_path, monkeypatch, _stamp_body(1, mode="full"), name="last-success-full")
    ok, msg = check.check_etcd_restore_drill()
    assert ok is False, "a full-mode stamp must not satisfy the list-only reader"
    assert "has ever passed" in msg


def test_etcd_drill_grace_is_derived_from_the_cron():
    """A grace period must come from the schedule it interacts with, never be picked round.

    The drill runs weekly (Monday 10:20). A window at or under the cadence flaps on every normal
    week; a window at twice it silently tolerates a whole missed run, which is the miss this
    check exists to catch — the 24h-grace-against-a-23h-gap failure of 2026-08-25, one cadence up.
    """
    defaults = yaml.safe_load(
        (
            Path(check.__file__).resolve().parents[3]
            / "setup"
            / "k3s"
            / "defaults"
            / "main.yml"
        ).read_text()
    )
    minute, hour, dom, month, dow = defaults["k3s_etcd_restore_drill_cron"].split()
    assert (dom, month) == ("*", "*") and dow != "*", (
        "this window is derived from a WEEKLY cadence; if the cron stops being weekly, "
        "ETCD_DRILL_MAX_AGE_S has to move with it"
    )
    cadence_s = 7 * 86400
    assert check.ETCD_DRILL_MAX_AGE_S > cadence_s, (
        "a window at or under the 7-day cadence flaps on every normal week"
    )
    assert check.ETCD_DRILL_MAX_AGE_S < 2 * cadence_s, (
        "a window of two cadences tolerates a fully missed run, which is exactly what this "
        "check is for"
    )


def test_etcd_drill_is_registered_and_can_actually_push():
    """Registration and the token must land together, and both now have.

    Asserting membership alone would pass for a check registered against a token nothing can
    set — which pushes to nowhere forever, present in the code and absent from the world. So
    this asserts the pair, in both directions: a later edit that drops either half fails here
    rather than quietly producing a monitor that cannot page.
    """
    names = {name for name, _, _ in check.CHECKS}
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    registered = "etcd_restore_drill" in names
    tokened = "KUMA_PUSH_ETCD_DRILL" in env_secret
    assert registered, "an unregistered check never runs; the reader would be dead code"
    assert registered == tokened, (
        "etcd_restore_drill's CHECKS entry and its KUMA_PUSH_ETCD_DRILL env-secret key move "
        "together — one without the other is either a check that cannot page or a token "
        "nothing reads"
    )


# ── the B2 storage line is a dashboard contract, not just a message ────────────────────────────
#
# B2 exposes no usage API, so `b2_storage`'s own log line is the ONLY record of stored bytes.
# The "B2 (off-site) — storage runway" panels on the Longhorn — Storage board parse it with a
# LogQL regexp. Rewording the message would leave those panels blank with no other signal —
# the same shape as a guard that goes textually inert. These bind the two together.

_TICK = chr(96)

_BOARD = (
    Path(__file__).resolve().parents[3]
    / "k8s"
    / "claude-otel"
    / "files"
    / "dashboards"
    / "Infrastructure"
    / "longhorn-storage.json"
)


def _dashboard_b2_regex() -> str:
    """The regexp the B2 runway panels parse the log line with, read from the board itself."""
    board = json.loads(_BOARD.read_text())
    for panel in board["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if "B2 storage" in expr and "| regexp " in expr:
                return expr.split("| regexp " + _TICK, 1)[1].split(_TICK, 1)[0]
    raise AssertionError(
        "no B2 storage regexp panel found on the Longhorn — Storage board"
    )


def test_the_b2_storage_line_still_matches_the_dashboard_regex():
    """The rejecting half is the message drifting: this fails the moment the wording changes."""
    ok, msg = check.b2_storage_verdict(
        used_bytes=5_100_000_000,
        versions=1110,
        truncated=False,
        cap=10_000_000_000,
        max_pct=90,
    )
    assert ok
    match = re.search(_dashboard_b2_regex(), msg)
    assert match, (
        f"the b2_storage message no longer matches the dashboard regexp: {msg!r}"
    )
    assert match.group("used_gb") == "5.10"
    assert match.group("cap_gb") == "10"
    assert match.group("pct") == "51"
    assert match.group("versions") == "1110"


def test_the_dashboard_regex_rejects_a_reworded_line():
    """The accepting half's mirror: a regexp loose enough to match anything would pass the test
    above while pinning nothing."""
    assert not re.search(_dashboard_b2_regex(), "B2 storage is fine, 1110 objects")


def test_every_b2_runway_panel_collapses_its_series():
    """Each unwrapped capture leaves the others as stream labels, so `versions` ticking over
    spawns a new series and the panel draws a staircase of one-point lines. Measured live: the
    unwrapped form returned 2 series for one logical value. avg() is what makes it one."""
    board = json.loads(_BOARD.read_text())
    exprs = [
        t["expr"]
        for panel in board["panels"]
        for t in panel.get("targets", [])
        if "B2 storage" in t.get("expr", "")
    ]
    assert exprs, "the B2 runway panels are gone from the board"
    for expr in exprs:
        assert expr.startswith("avg(avg_over_time("), expr
