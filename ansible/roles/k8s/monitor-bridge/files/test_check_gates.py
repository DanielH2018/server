"""The run-loop gates: what suppresses a check rather than what the check decides.

A monitor that cannot reach Prometheus, Loki or B2 must report "cannot tell", never "down" —
otherwise one unreachable dependency fires every monitor that reads it at once. The startup
grace and the down-streak hysteresis are the same idea over time: a check that has just come
up, or has been down once, is not yet evidence.

These are the tests that drive `run_once()` end to end with the transport stubbed, so they are
the ones that fail when the wiring changes rather than the logic.
"""

import importlib
from pathlib import Path

import pytest

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


def test_dual_estate_checks_all_pin_the_origin():
    # The five checks whose metrics genuinely exist in BOTH estates (container_start_time_seconds,
    # container_oom_events_total, the container_cpu_cfs_* pair, and `up`). If a new call site is
    # added to one of these without origin_sel, it widens to the whole homelab the moment
    # PROMETHEUS_URL moves — and reports k8s pods as daniel-server offenders.
    source = Path(check.__file__).read_text()
    for metric in (
        "container_start_time_seconds",
        "container_oom_events_total",
        "container_cpu_cfs_throttled_periods_total",
        "container_cpu_cfs_periods_total",
        "container_cpu_cfs_throttled_seconds_total",
    ):
        # A hardcoded `{` straight after the metric name means the matchers bypassed origin_sel.
        assert metric + "{" not in source, (
            "%s uses a literal label block; it must go through origin_sel() so the estate pin "
            "is applied" % metric
        )


def test_duration_seconds_parses_prometheus_durations():
    assert check.duration_seconds("15m") == 900
    assert check.duration_seconds("1h") == 3600
    assert check.duration_seconds("90s") == 90
    assert check.duration_seconds("1d") == 86400
    for bad in ("", "15", "m", "1y", "abc"):
        with pytest.raises(ValueError):
            check.duration_seconds(bad)


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
    monkeypatch.setattr(check, "log", lambda *a, **k: None)
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
    """prom_vector stub keyed by substring of the query."""

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
