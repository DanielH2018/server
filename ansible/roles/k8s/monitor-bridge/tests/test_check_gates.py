"""The run-loop gates: what suppresses a check rather than what the check decides.

A monitor that cannot reach Prometheus, Loki or B2 must report "cannot tell", never "down" —
otherwise one unreachable dependency fires every monitor that reads it at once. The startup
grace and the down-streak hysteresis are the same idea over time: a check that has just come
up, or has been down once, is not yet evidence.

These are the tests that drive `run_once()` end to end with the transport stubbed, so they are
the ones that fail when the wiring changes rather than the logic.
"""

import importlib
import urllib.error
from pathlib import Path

import pytest

import bridge_common
import bridge_parsing
import bridge_config
import bridge_io
import checks_b2
import checks_cluster
import check

_REPO = Path(__file__).resolve().parents[5]


def test_loki_reachable_ok(monkeypatch):
    monkeypatch.setattr(
        bridge_io, "_get_json", lambda *a, **k: {"status": "success", "data": ["job"]}
    )
    assert bridge_io.loki_reachable() is True
    ok, msg = check.check_loki_reachable()
    assert ok
    assert "reachable" in msg.lower()


def test_loki_reachable_non_success_raises(monkeypatch):
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        bridge_io.loki_reachable()


# ── Prometheus reachability gate + alert-storm suppression (L1) ──────────────


def test_check_prometheus_reachable(monkeypatch):
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda q: 1.0)
    ok, msg = check.check_prometheus()
    assert ok
    assert "reachable" in msg.lower()


def test_check_prometheus_no_data_is_down(monkeypatch):
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda q: None)
    ok, _msg = check.check_prometheus()
    assert not ok


def _wire_run_once(monkeypatch, prom_result):
    """Drive run_once with a tiny CHECKS list (one prom-dependent, one not) and capture pushes.

    Returns (ran, pushes): `ran` is the names of checks actually executed, `pushes` is
    [(token, ok, msg), ...] in push order (incl. the leading `prometheus` push).
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        bridge_io, "push", lambda token, ok, msg: pushes.append((token, ok, msg))
    )
    if isinstance(prom_result, Exception):

        def _prom():
            raise prom_result
    else:

        def _prom():
            return prom_result

    monkeypatch.setattr(check, "check_prometheus", _prom)
    # No exporters down by default, so the prom-up path doesn't hit the network probing `up`.
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
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
    monkeypatch.setattr(bridge_io, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
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


# `test_b2_dependent_excludes_backup` was deleted here on 2026-09-01. It asserted `"backup" not
# in check.B2_DEPENDENT` to keep the B2 gate from suppressing the one check that polled B2's real
# state — sound while it was written, dead since Kopia retired on 2026-08-13 (ADR-0014) and backup
# moved to Longhorn. No check named `backup` exists in CHECKS or STARTUP_GRACE any more, so the
# assertion could not fail, and its comment described two behaviours that had stopped being true.
# Both invariants it pointed at are still enforced, by name: `B2_DEPENDENT <= names` directly
# above, and the STARTUP_GRACE disjointness in
# test_check_streaks.py::test_startup_grace_disjoint_from_run_once_skip_sets.


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
    ok, msg = checks_cluster.k8s_workloads_verdict(None, [], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_workloads_partial_series_is_down():
    # A partially-loaded kube-state-metrics — e.g. `apps` dropped from its scoped ClusterRole,
    # which takes every deployment series away while the pod stays up and Ready.
    ok, msg = checks_cluster.k8s_workloads_verdict(2, [], 5)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_workloads_healthy_when_series_present_and_none_unavailable():
    ok, msg = checks_cluster.k8s_workloads_verdict(18, [], 5)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_k8s_workloads_names_the_offenders():
    offenders = [
        ({"deployment": "n8n-runners"}, 1.0),
        ({"deployment": "registry"}, 2.0),
    ]
    ok, msg = checks_cluster.k8s_workloads_verdict(18, offenders, 5)
    assert ok is False
    # Sorted, so the message is stable rather than dependent on Prometheus' series order.
    assert "n8n-runners(1), registry(2)" in msg


def test_k8s_workloads_crash_loop_is_down_despite_available_replicas():
    # The 2026-08-13 homepage incident: a CrashLoopBackOff pod passes readiness for a brief
    # window each backoff cycle, so replica availability read healthy through 31 restarts.
    # The restart counter is the signal that doesn't flap.
    restarts = [({"pod": "homepage-58d867556f-7qbz9"}, 6.0)]
    ok, msg = checks_cluster.k8s_workloads_verdict(18, [], 5, restarts)
    assert ok is False
    assert "crash-looping" in msg
    assert "homepage-58d867556f-7qbz9(6)" in msg


def test_k8s_workloads_unavailable_replicas_outrank_the_restart_arm():
    # Both arms firing is one incident; the replica message is the more actionable one.
    offenders = [({"deployment": "homepage"}, 1.0)]
    restarts = [({"pod": "homepage-x"}, 6.0)]
    ok, msg = checks_cluster.k8s_workloads_verdict(18, offenders, 5, restarts)
    assert ok is False
    assert "unavailable replicas" in msg


def test_k8s_daemonsets_absent_series_is_down_not_up():
    # Same fail-closed shape as the deployment arm: an absent DaemonSet series is UNKNOWN,
    # not "no DaemonSets have a problem".
    ok, msg = checks_cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=None, min_daemonsets=9
    )
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_daemonsets_partial_series_is_down():
    ok, msg = checks_cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=3, min_daemonsets=9
    )
    assert ok is False
    assert "below the floor" in msg


def test_k8s_daemonsets_names_the_offenders():
    ds_offenders = [({"daemonset": "otel-collector"}, 1.0)]
    ok, msg = checks_cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=9, ds_offenders=ds_offenders, min_daemonsets=9
    )
    assert ok is False
    assert "otel-collector(1)" in msg


def test_k8s_daemonsets_healthy_alongside_healthy_deployments():
    ok, msg = checks_cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=9, min_daemonsets=9
    )
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_origin_sel_is_empty_without_a_pin(monkeypatch):
    # Against the Docker Prometheus there is no `origin` label at all — external_labels apply on
    # remote-write and never to local storage — so a pin there would select NOTHING and read as
    # healthy. Empty must stay empty.
    monkeypatch.setattr(bridge_config, "PROM_ORIGIN", "")
    assert bridge_io.origin_sel() == ""
    assert bridge_io.origin_sel('name!=""') == '{name!=""}'


def test_origin_sel_appends_the_pin(monkeypatch):
    monkeypatch.setattr(bridge_config, "PROM_ORIGIN", 'origin="daniel-server"')
    assert bridge_io.origin_sel() == '{origin="daniel-server"}'
    assert bridge_io.origin_sel('name!=""') == '{name!="", origin="daniel-server"}'


def test_origin_pin_derives_from_the_prometheus_url(monkeypatch):
    # THE regression this guards. PROM_ORIGIN is derived rather than configured precisely so it
    # cannot drift out of lockstep with PROMETHEUS_URL: pointing one at the cluster and forgetting
    # the other selects nothing, which every one of these checks decodes as healthy.
    monkeypatch.setenv("PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(bridge_config)
    try:
        assert reloaded.PROM_ORIGIN == 'origin="daniel-server"'
    finally:
        monkeypatch.undo()
        importlib.reload(bridge_config)


def test_origin_pin_absent_when_reading_the_docker_prometheus(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("CLUSTER_PROMETHEUS_URL", "https://prom-k8s.example")
    monkeypatch.delenv("PROM_ORIGIN", raising=False)
    reloaded = importlib.reload(bridge_config)
    try:
        assert reloaded.PROM_ORIGIN == ""
    finally:
        monkeypatch.undo()
        importlib.reload(bridge_config)


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

    monkeypatch.setattr(bridge_config, "CLUSTER_PROM_URL", "https://cluster")
    monkeypatch.setattr(bridge_io, "prom_vector", fake_vector)
    ok, _ = checks_cluster.check_cluster_targets()
    assert ok is True
    assert seen["q"] == 'up{origin!="daniel-server"}'
    assert seen["base"] == "https://cluster"


def test_cluster_targets_empty_is_down():
    ok, msg = checks_cluster.targets_verdict([], bridge_config.CLUSTER_TARGETS_MIN)
    assert ok is False
    assert "UNKNOWN" in msg


def test_cluster_targets_disabled_without_cluster_url(monkeypatch):
    monkeypatch.setattr(bridge_config, "CLUSTER_PROM_URL", "")
    ok, msg = checks_cluster.check_cluster_targets()
    assert ok is True
    assert "disabled" in msg


def test_targets_empty_vector_is_down_not_all_clear():
    # THE hole B5 opens. Before the repoint an empty `up` could only mean the queried Prometheus
    # was down, and the PROM_DEPENDENT gate suppressed this check first. Against the cluster copy
    # the gate passes (that Prometheus is fine) while `up{origin="daniel-server"}` is empty, and
    # the old code returned "all 0 targets up".
    ok, msg = checks_cluster.targets_verdict([], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_targets_below_floor_is_down():
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    ok, msg = checks_cluster.targets_verdict(vec, 5)
    assert ok is False
    assert "below the floor" in msg


def test_targets_names_down_jobs_above_the_floor():
    vec = [({"job": "node"}, 0.0)] + [({"job": "j%d" % i}, 1.0) for i in range(5)]
    ok, msg = checks_cluster.targets_verdict(vec, 5)
    assert ok is False
    assert "1 target(s) down: node" in msg


def test_targets_all_up_above_the_floor():
    vec = [({"job": "j%d" % i}, 1.0) for i in range(11)]
    ok, msg = checks_cluster.targets_verdict(vec, 5)
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
    # Every runtime module, not just check.py: the checks are moving out by domain, and a
    # literal label block in a moved check must stay as visible as one that never moved.
    files = Path(check.__file__).resolve().parent
    source = "".join(
        p.read_text()
        for p in sorted(files.glob("*.py"))
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )
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
    if bridge_config.PROM_ORIGIN:
        assert bridge_config.PROM_ORIGIN not in bridge_io.cadvisor_sel(
            'container!=""'
        ), (
            "cadvisor_sel() must not apply the origin pin — cAdvisor series carry no origin label"
        )
    # Independent of the environment: the pin can only enter through PROM_ORIGIN, so a
    # cadvisor_sel() built with a sentinel pin must still come back without it.
    saved = bridge_config.PROM_ORIGIN
    try:
        bridge_config.PROM_ORIGIN = 'origin="sentinel"'
        assert "sentinel" not in bridge_io.cadvisor_sel('container!=""')
        assert "sentinel" in bridge_io.origin_sel('container!=""')
    finally:
        bridge_config.PROM_ORIGIN = saved


def test_up_still_pins_the_origin_where_the_label_exists():
    # The other half of the contract: `up` DOES carry origin (the node job is relabelled), and
    # targets_verdict depends on the pin to scope its floor to one estate. Dropping it there would
    # make check_targets a duplicate of check_cluster_targets and orphan daniel-server's
    # node-exporter, which is why the fix deliberately left this call site alone.
    if bridge_config.PROM_ORIGIN:
        assert bridge_config.PROM_ORIGIN in bridge_io.origin_sel(), (
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
    monkeypatch.setattr(bridge_config, "CLUSTER_PROM_URL", "")
    ok, msg = checks_cluster.check_k8s_workloads()
    assert ok is True
    assert "disabled" in msg


def test_cluster_prometheus_gate_down_when_no_result(monkeypatch):
    monkeypatch.setattr(bridge_config, "CLUSTER_PROM_URL", "https://prom-k8s.example")
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda *a, **k: None)
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
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    monkeypatch.setattr(
        bridge_io, "push", lambda token, ok, msg: pushed.__setitem__(token, (ok, msg))
    )
    monkeypatch.setattr(bridge_common, "log", lambda *a, **k: None)
    check.run_once()
    return pushed


def _reset_b2_probe(
    monkeypatch, key_id="kid", app_key="akey", interval=1800, transport_retry=300
):
    monkeypatch.setattr(bridge_config, "B2_PROBE_KEY_ID", key_id)
    monkeypatch.setattr(bridge_config, "B2_PROBE_APPLICATION_KEY", app_key)
    monkeypatch.setattr(bridge_config, "B2_PROBE_INTERVAL_S", interval)
    monkeypatch.setattr(bridge_config, "B2_TRANSPORT_RETRY_S", transport_retry)
    monkeypatch.setattr(
        checks_b2,
        "_b2_probe",
        {"ts": 0.0, "ok": True, "msg": "not yet probed", "ttl": interval},
    )


def _cap_denial():
    """The exception a real transaction-cap breach raises.

    _get_json re-raises urllib HTTPError UNTOUCHED (check.py, "Re-raise the SAME type") and wraps
    only non-HTTP failures as RuntimeError. b2_reachable now branches on exactly that distinction
    to pick a cache TTL, so a test that fakes a 403 as a RuntimeError would exercise the transport
    path and prove the opposite of what it claims. These tests used to do that.
    """
    return urllib.error.HTTPError(
        bridge_config.B2_PROBE_URL,
        403,
        "Forbidden: transaction_cap_exceeded",
        {},
        None,
    )


def test_b2_reachable_disabled_without_credentials(monkeypatch):
    _reset_b2_probe(monkeypatch, key_id="", app_key="")
    ok, msg = checks_b2.b2_reachable(now=10_000)
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
    monkeypatch.setattr(bridge_config, "B2_PROBE_KEY_ID", "kid")
    monkeypatch.setattr(bridge_config, "B2_PROBE_APPLICATION_KEY", "akey")
    monkeypatch.setattr(bridge_io, "_get_json", lambda url, headers=None: response)
    result_ok, msg = checks_b2.b2_authorize()
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_b2_reachable_surfaces_the_cap_error_text(monkeypatch):
    # G3: the alert must name the CAUSE. B2 answers a cap breach with transaction_cap_exceeded,
    # and _get_json appends the response body to the HTTPError, so it has to reach the message.
    _reset_b2_probe(monkeypatch)

    def _boom(url, headers=None):
        raise _cap_denial()

    monkeypatch.setattr(bridge_io, "_get_json", _boom)
    ok, msg = checks_b2.b2_reachable(now=10_000)
    assert ok is False and "transaction_cap_exceeded" in msg


def test_b2_reachable_caches_failure_and_does_not_reprobe(monkeypatch):
    # THE cost-critical property. The fault being detected is a transaction cap, so a failure must
    # NOT re-probe every cycle the way email_backstop does — that would spend the exhausted budget.
    _reset_b2_probe(monkeypatch)
    calls = []

    def _boom(url, headers=None):
        calls.append(url)
        raise _cap_denial()

    monkeypatch.setattr(bridge_io, "_get_json", _boom)
    first_ok, _ = checks_b2.b2_reachable(now=10_000)
    # five more cycles inside the interval (INTERVAL=300 -> 25 min of cycles)
    for offset in (300, 600, 900, 1200, 1500):
        ok, msg = checks_b2.b2_reachable(now=10_000 + offset)
        assert ok is False
        assert (
            "transaction_cap_exceeded" in msg
        )  # cached verdict still reported every cycle
    assert first_ok is False
    assert len(calls) == 1, "a cached failure must not re-probe: %d calls" % len(calls)


def test_b2_reachable_reprobes_a_transport_failure_next_cycle(monkeypatch):
    # The REJECT half of the caching pair above, and the 2026-08-30 restart's fix. A failure that
    # never reached B2 was billed nothing, so the cost argument that justifies the 30-minute cache
    # does not apply to it — and holding it pinned the gate DOWN for 25 minutes against an 8m35s
    # outage, because the cache was holding back the RECOVERY as well as the retry.
    _reset_b2_probe(monkeypatch, interval=1800, transport_retry=300)
    calls = []
    # _get_json wraps DNS/connect/timeout failures as RuntimeError; only these take the short TTL.
    outcomes = [RuntimeError("b2 api: Temporary failure in name resolution")]

    def _flaky(url, headers=None):
        calls.append(url)
        if outcomes:
            raise outcomes.pop(0)
        return {"accountId": "a1"}

    monkeypatch.setattr(bridge_io, "_get_json", _flaky)
    ok, msg = checks_b2.b2_reachable(now=10_000)
    assert ok is False and "name resolution" in msg
    # One cycle later the transport TTL has expired, so the gate re-probes and recovers — where a
    # cap denial would still be reporting its cached verdict for another 25 minutes.
    ok, msg = checks_b2.b2_reachable(now=10_300)
    assert ok is True, "a transport failure must re-probe next cycle, got: %s" % msg
    assert len(calls) == 2, "expected a re-probe, got %d calls" % len(calls)


def test_b2_transport_retry_is_shorter_than_the_probe_interval():
    # The two TTLs must not converge: if the transport retry ever reached B2_PROBE_INTERVAL_S the
    # split above would be a no-op that still reads as implemented.
    assert bridge_config.B2_TRANSPORT_RETRY_S < bridge_config.B2_PROBE_INTERVAL_S


def test_b2_reachable_reprobes_after_the_interval(monkeypatch):
    _reset_b2_probe(monkeypatch, interval=1800)
    calls = []

    def _ok(url, headers=None):
        calls.append(url)
        return {"accountId": "a1"}

    monkeypatch.setattr(bridge_io, "_get_json", _ok)
    checks_b2.b2_reachable(now=10_000)
    checks_b2.b2_reachable(now=10_000 + 1799)  # still cached
    assert len(calls) == 1
    checks_b2.b2_reachable(now=10_000 + 1801)  # interval elapsed
    assert len(calls) == 2


def _wire_run_once_b2(monkeypatch, b2_result, checks, b2_dependent):
    """Drive run_once with Prometheus+Loki UP and a stubbed B2-reachability result."""
    ran, pushes = [], []
    monkeypatch.setattr(bridge_io, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
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
