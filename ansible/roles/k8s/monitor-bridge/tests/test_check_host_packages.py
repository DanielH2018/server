"""`host_packages` (Host Packages — Status): the host-package builder's two page arms.

The builder runs on daniel-box every 2 minutes and publishes two textfile series through the
node exporter. The fetch timestamp ages when the timer dies, the token is missing or `git fetch`
keeps failing, because a failed fetch writes nothing. The unbuilt-since series carries the cause
in its `reason` and `sha` labels, and its value is non-zero while a commit stays unbuilt. In each
unbuilt case the builder exits 0, so only this row reports it. The exporter-suppression pair is
in test_check_gates_exporters.py.
"""

import pytest

import checks.gitops
from _fake_sources import FakeSources

NOW = 1_780_000_000.0
SHA = "0123456789ab"


def _unbuilt(since, reason="ok", sha=SHA):
    return ({"reason": reason, "sha": sha}, float(since))


def _run(cfg, fetch_age_s, unbuilt):
    """Run the check with a fetch `fetch_age_s` old (None = absent) and these unbuilt series."""
    fetch = None if fetch_age_s is None else NOW - fetch_age_s
    src = FakeSources(prom_scalar=lambda q: fetch, prom_vector=lambda q: unbuilt)
    return checks.gitops.check_host_packages(cfg, src, now=NOW)


def test_the_bounds_are_the_plans_defaults(cfg):
    assert cfg.BUILDER_MAX_FETCH_AGE_S == 1800
    assert cfg.BUILDER_MAX_LAG_S == 7200


@pytest.mark.parametrize(
    ("age_s", "ok"),
    [
        pytest.param(31 * 60, False, id="fetch_31m_old_is_red"),
        pytest.param(60, True, id="fetch_1m_old_is_green"),
        pytest.param(30 * 60, True, id="fetch_at_the_bound_is_green"),
        pytest.param(None, False, id="fetch_absent_is_red"),
    ],
)
def test_the_fetch_arm(cfg, age_s, ok):
    result_ok, msg = _run(cfg, age_s, [_unbuilt(0)])
    assert result_ok is ok, msg
    if not ok:
        assert "fetch" in msg


def test_a_stale_fetch_names_the_reason_and_sha_of_the_series_beside_it(cfg):
    ok, msg = _run(cfg, 31 * 60, [_unbuilt(0, reason="hold")])
    assert not ok
    assert "31m" in msg and "reason=hold" in msg and f"sha={SHA}" in msg


@pytest.mark.parametrize(
    ("past_bound_s", "ok"),
    [
        pytest.param(+60, False, id="unbuilt_past_the_bound_is_red"),
        pytest.param(-60, True, id="unbuilt_inside_the_bound_is_green"),
    ],
)
def test_the_lag_arm(cfg, past_bound_s, ok):
    since = NOW - cfg.BUILDER_MAX_LAG_S - past_bound_s
    result_ok, msg = _run(cfg, 60, [_unbuilt(since, reason="no-green")])
    assert result_ok is ok, msg
    # Green or red, the message names why the commit is unbuilt.
    assert "reason=no-green" in msg and f"sha={SHA}" in msg


def test_zero_unbuilt_is_green_whatever_the_reason(cfg):
    # A hold at a caught-up tip leaves nothing unbuilt, so it is not a page.
    ok, msg = _run(cfg, 60, [_unbuilt(0, reason="hold")])
    assert ok, msg


def test_the_oldest_unbuilt_series_is_judged_when_a_label_change_leaves_two(cfg):
    old = NOW - cfg.BUILDER_MAX_LAG_S - 600
    fresh = _unbuilt(NOW - 60, reason="ok", sha="newsha")
    ok, msg = _run(cfg, 60, [fresh, _unbuilt(old, reason="ci", sha="oldsha")])
    assert not ok
    assert "reason=ci" in msg and "sha=oldsha" in msg


def test_an_absent_unbuilt_series_is_red(cfg):
    ok, msg = _run(cfg, 60, [])
    assert not ok and "unbuilt-since series absent" in msg


def test_both_arms_are_named_when_both_fire(cfg):
    stuck = _unbuilt(NOW - cfg.BUILDER_MAX_LAG_S - 60, reason="keyring")
    ok, msg = _run(cfg, 31 * 60, [stuck])
    assert not ok
    assert "last fetched" in msg and "master unbuilt" in msg and "reason=keyring" in msg


def test_it_reads_the_builder_series_with_no_daniel_server_pin(cfg):
    # origin_sel() would pin origin="daniel-server" (PROM_ORIGIN), and only daniel-box runs the
    # builder: the pinned query matches nothing, and the tile reads red for ever.
    src = FakeSources(
        prom_scalar=lambda q: NOW - 60, prom_vector=lambda q: [_unbuilt(0)]
    )
    checks.gitops.check_host_packages(cfg, src, now=NOW)
    queries = src.queries("prom_scalar") + src.queries("prom_vector")
    assert any("homelab_builder_fetch_timestamp_seconds" in q for q in queries)
    assert any("homelab_builder_unbuilt_since_timestamp_seconds" in q for q in queries)
    assert not any("origin=" in q for q in queries), queries


# Reconcile drift (H7, *Which outcomes hold the host*): named in the message, never a verdict.


def _drift(host, arm, count):
    return ({"host": host, "arm": arm}, float(count))


def _run_with_drift(cfg, fetch_age_s, unbuilt, drift):
    fetch = NOW - fetch_age_s
    rows = {"homelab_builder_unbuilt": unbuilt, "homelab_host_reconcile_drift": drift}
    src = FakeSources(
        prom_scalar=lambda q: fetch,
        prom_vector=lambda q: next(v for k, v in rows.items() if k in q),
    )
    return checks.gitops.check_host_packages(cfg, src, now=NOW), src


def test_a_non_zero_drift_count_is_named_in_the_message_without_going_down(cfg):
    (ok, msg), _ = _run_with_drift(
        cfg,
        60,
        [_unbuilt(0)],
        [_drift("daniel-pi", "ufw", 2), _drift("daniel-box", "legacy", 1)],
    )
    assert ok, msg
    assert "drift: daniel-box legacy=1, daniel-pi ufw=2" in msg


def test_drift_is_named_on_a_red_verdict_too_and_does_not_cause_it(cfg):
    (ok, msg), _ = _run_with_drift(
        cfg, 31 * 60, [_unbuilt(0)], [_drift("daniel-pi", "ufw", 1)]
    )
    assert not ok
    assert "fetch" in msg and "drift: daniel-pi ufw=1" in msg


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param([], id="no-series"),
        pytest.param([_drift("daniel-pi", "ufw", 0)], id="zero"),
        pytest.param([({"reason": "ok", "sha": SHA}, 5.0)], id="not-a-drift-series"),
    ],
)
def test_no_drift_leaves_the_message_as_it_was(cfg, drift):
    (ok, msg), _ = _run_with_drift(cfg, 60, [_unbuilt(0)], drift)
    assert ok and "drift" not in msg


def test_the_drift_query_names_no_job_because_the_pi_scrapes_as_node_pi(cfg):
    _, src = _run_with_drift(cfg, 60, [_unbuilt(0)], [])
    queries = [
        q for q in src.queries("prom_vector") if "homelab_host_reconcile_drift" in q
    ]
    assert queries and not any("job=" in q or "origin=" in q for q in queries), queries
