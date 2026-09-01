"""Health verdicts for the hardware and the hosts: disks, certs, SMART, the UPS, the Pi.

These read a sensor and decide. The decision is the whole test — the HTTP glue is exercised
live by `check.py --once` at deploy time, and the run-loop wiring is in test_check_gates.py.
"""

import pytest

import bridge_parsing
import bridge_config
import bridge_io
import check


def test_disk_under_threshold_is_ok(monkeypatch):
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-box"}, 50.0),
            ({"origin": "daniel-server"}, 44.0),
        ],
    )
    ok, msg = check.check_disk()
    assert ok
    assert "under" in msg


def test_disk_over_threshold_names_mount(monkeypatch):
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-box"}, 95.0),
            ({"origin": "daniel-server"}, 12.0),
        ],
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "/" in msg
    assert "95" in msg


def test_disk_names_the_breaching_host_not_the_healthy_one(monkeypatch):
    """THE BUG THIS PINS (2026-08-15): avail and size were two separate max() queries, so once
    both estates reported into one Prometheus a full disk on one host could be paired with the
    other's size. A per-origin percentage keeps each host's numerator with its own denominator,
    and the alert has to name WHICH host is full to be actionable."""
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 96.0),
            ({"origin": "daniel-box"}, 24.0),
        ],
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "daniel-server" in msg
    assert "daniel-box" not in msg


def test_disk_groups_by_origin_so_neither_host_is_unwatched(monkeypatch):
    seen = {}
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])

    def fake_vector(promql):
        seen["q"] = promql
        return [({"origin": "daniel-box"}, 10.0)]

    monkeypatch.setattr(bridge_io, "prom_vector", fake_vector)
    check.check_disk()
    assert "by (origin)" in seen["q"]
    # The division must be inside the query, so the two series are paired by Prometheus on all
    # their labels rather than by two independent aggregates here.
    assert "node_filesystem_avail_bytes" in seen["q"]
    assert "node_filesystem_size_bytes" in seen["q"]


def test_disk_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
    ok, msg = check.check_disk()
    assert not ok
    assert "unavailable" in msg


def test_mem_names_the_breaching_host(monkeypatch):
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 92.0),
            ({"origin": "daniel-box"}, 30.0),
        ],
    )
    ok, msg = check.check_mem()
    assert not ok
    assert "daniel-server" in msg


def test_mem_reports_the_worst_host_when_all_are_healthy(monkeypatch):
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: [
            ({"origin": "daniel-server"}, 41.0),
            ({"origin": "daniel-box"}, 63.0),
        ],
    )
    ok, msg = check.check_mem()
    assert ok
    assert "63" in msg


def test_mem_metric_unavailable_alerts(monkeypatch):
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: [])
    ok, msg = check.check_mem()
    assert not ok
    assert "unavailable" in msg


@pytest.mark.parametrize(
    ("days_left", "ok", "expect"),
    [
        (30.0, True, "valid"),  # default CERT_MIN_DAYS=14; 30 days left -> ok
        (5.0, False, "expires"),  # 5 days left < 14 -> down
        (None, False, "unavailable"),
    ],
)
def test_cert(monkeypatch, days_left, ok, expect):
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda *a, **k: days_left)
    result_ok, msg = check.check_cert()
    assert result_ok is ok
    assert expect in msg


# ── scrutiny SMART-data freshness (collector runs daily; web API holds last report) ──


def _reset_origin_streaks():
    check._host_origin_streaks.clear()


def test_mem_pages_when_a_host_stops_reporting(monkeypatch):
    _reset_origin_streaks()
    monkeypatch.setattr(bridge_config, "HOST_ORIGINS_CONSECUTIVE", 1)
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: [({"origin": "daniel-server"}, 21.0)]
    )
    ok, msg = check.check_mem()
    assert not ok
    assert "1 of 2" in msg
    assert "daniel-server" in msg


def test_disk_pages_when_a_host_stops_reporting(monkeypatch):
    _reset_origin_streaks()
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(bridge_config, "HOST_ORIGINS_CONSECUTIVE", 1)
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: [({"origin": "daniel-server"}, 30.0)]
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "1 of 2" in msg


def test_a_reboot_length_shortfall_does_not_page(monkeypatch):
    """The weekly reboot removes a node's node-exporter for minutes against a 5m check loop, so a
    bare floor would page every Sunday. Only the HOST_ORIGINS_CONSECUTIVE'th cycle fails."""
    _reset_origin_streaks()
    monkeypatch.setattr(bridge_config, "HOST_ORIGINS_CONSECUTIVE", 3)
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: [({"origin": "daniel-server"}, 21.0)]
    )
    assert check.check_mem()[0] is True
    assert check.check_mem()[0] is True
    assert check.check_mem()[0] is False


def test_full_coverage_resets_the_shortfall_streak(monkeypatch):
    _reset_origin_streaks()
    monkeypatch.setattr(bridge_config, "HOST_ORIGINS_CONSECUTIVE", 2)
    one = [({"origin": "daniel-server"}, 21.0)]
    both = [({"origin": "daniel-server"}, 21.0), ({"origin": "daniel-box"}, 30.0)]
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: one)
    assert check.check_mem()[0] is True
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: both)
    assert check.check_mem()[0] is True
    monkeypatch.setattr(bridge_io, "prom_vector", lambda q: one)
    assert check.check_mem()[0] is True


def test_a_breaching_present_host_outranks_the_coverage_complaint(monkeypatch):
    """A survivor that is genuinely full must still page as full. Ordering the floor ahead of the
    breach scan would have replaced a real disk-full alert with 'only 1 of 2 hosts reporting'."""
    _reset_origin_streaks()
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(bridge_config, "HOST_ORIGINS_CONSECUTIVE", 1)
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: [({"origin": "daniel-server"}, 97.0)]
    )
    ok, msg = check.check_disk()
    assert not ok
    assert "97" in msg

    _reset_origin_streaks()
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: [({"origin": "daniel-server"}, 99.0)]
    )
    ok, msg = check.check_mem()
    assert not ok
    assert "99" in msg


def test_crash_loop_arm_gates_on_a_recent_restart_not_just_the_hour_window(monkeypatch):
    """A recovered pod must drop out of the arm instead of holding the tile red for an hour.

    `increase(...[1h]) > 3` is a pure lookback, so zigbee2mqtt kept `k3s Workload Health` DOWN
    on `restarts in window: 9` for ~30 min after it recovered on 2026-08-23. The recency clause
    is the fix and it is invisible to k8s_workloads_verdict, which receives the offenders as an
    already-filtered list — the query text is the only place it can be enforced. Verified live
    the same day: with a 2m recency window the recovered pod dropped out while the ungated
    query still matched it.
    """
    queries = []

    def record(promql, *a, **k):
        queries.append(promql)
        return []

    monkeypatch.setattr(bridge_config, "CLUSTER_PROM_URL", "http://cluster-prom:9090")
    monkeypatch.setattr(bridge_io, "prom_vector", record)
    monkeypatch.setattr(bridge_io, "prom_scalar", lambda *a, **k: 66.0)
    check.check_k8s_workloads()

    restart_queries = [q for q in queries if "status_restarts_total" in q]
    assert len(restart_queries) == 1
    q = restart_queries[0]
    # Both windows present, and the recency one joined with `and` so it filters rather than
    # replaces — an `or` here would widen the arm instead of narrowing it.
    assert "[%s]" % bridge_config.K8S_RESTART_WINDOW in q
    assert "[%s]) > 0" % bridge_config.K8S_RESTART_RECENT_WINDOW in q
    assert " and " in q
    assert " or " not in q


def test_the_recency_window_is_wider_than_the_worst_observed_restart_spacing():
    """Below the spacing the arm flaps, and `k3s Workload Health` is max_retries: 0.

    The 2026-08-13 homepage incident spread 31 restarts over a night, ~15-19 min apart. A
    recency window inside that spacing goes UP in the gaps, and every flap is an immediate
    DOWN plus a notification (the crowdsec-appsec failure: 24 transitions in 3h). Pin the
    floor so a later "make it clear faster" edit cannot cross it silently.
    """
    # 30m, not 20m: the observed spacing RANGE tops out at ~19 min, so a 20m floor sits at the
    # edge of the flapping band rather than outside it — and would let a later edit to 20m or
    # 25m pass the very test written to stop it.
    assert (
        bridge_parsing.duration_seconds(bridge_config.K8S_RESTART_RECENT_WINDOW)
        >= 30 * 60
    )
    # And it must still be shorter than the evidence window, or it gates nothing.
    assert bridge_parsing.duration_seconds(
        bridge_config.K8S_RESTART_RECENT_WINDOW
    ) < bridge_parsing.duration_seconds(bridge_config.K8S_RESTART_WINDOW)


def test_host_origins_floor_defaults_to_both_nodes():
    """The floor is 2 because node-exporter is a DaemonSet on both nodes. At 1 the arm is inert
    and check_disk/check_mem silently report the survivor's numbers as the estate's — which is
    precisely the 2026-08-23 outage it was added for, where daniel-box went unwatched for 5.4h
    behind two green tiles. Every other arm here monkeypatches the constant, so nothing pinned
    the shipped value (2026-08-23b review L3)."""
    assert bridge_config.HOST_ORIGINS_MIN == 2, (
        "HOST_ORIGINS_MIN must default to 2 — one per node. Below that the host-coverage arm "
        "cannot fire and both host checks go back to monitoring whichever node still reports."
    )


def test_host_origins_floor_is_overridable_from_the_env_secret():
    """It must be a rendered key, not just a code default: a planned single-node maintenance
    window otherwise turns check_disk and check_mem permanently red with no way to stand them
    down. A one-way door is a bug even when the door is a threshold."""
    import pathlib

    env_secret = (
        pathlib.Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    )
    assert 'HOST_ORIGINS_MIN: "2"' in env_secret.read_text(), (
        "HOST_ORIGINS_MIN must be rendered in env-secret.yaml.j2 so an operator can lower it "
        "for a maintenance window and put it back, rather than editing check.py."
    )


# ── the Pi is excluded from the host-level node_* checks ──────────────────────────────────
#
# daniel-pi runs node-exporter like the other two hosts, so node_memory_* and
# node_filesystem_* carry its series. check_pi_pressure already owns Pi disk and memory,
# against thresholds written for a 456 MB box — at rest the Pi sits near 65% memory used,
# well inside check_mem's 90% but with little room, so without the exclusion an ordinary
# working Pi pages check_mem AND check_pi_pressure for one fact.
#
# These assert the QUERY, not the verdict. A verdict test passes either way whenever the
# stub returns no Pi series, which is exactly the shape that would let the exclusion be
# dropped without a red test.


def test_disk_query_excludes_the_pi_origin(monkeypatch):
    seen = []
    monkeypatch.setattr(bridge_config, "DISK_MOUNTPOINTS", ["/"])
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: (seen.append(q), [({"origin": "daniel-box"}, 10.0)])[1],
    )

    check.check_disk()

    assert seen, "check_disk must query Prometheus"
    assert 'origin!~"daniel-pi"' in seen[0], (
        "check_disk must exclude the Pi — check_pi_pressure owns its disk"
    )


def test_mem_query_excludes_the_pi_origin(monkeypatch):
    seen = []
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda q: (seen.append(q), [({"origin": "daniel-box"}, 10.0)])[1],
    )

    check.check_mem()

    assert seen, "check_mem must query Prometheus"
    assert seen[0].count('origin!~"daniel-pi"') == 2, (
        "BOTH sides of the MemAvailable/MemTotal division need the matcher — the division "
        "matches element-wise on labels, so excluding one side alone drops every series and "
        "the check reports 'memory metric unavailable' instead of a reading"
    )


def test_the_exclusion_keeps_series_that_carry_no_origin_label():
    """`!~` on a named host must not filter out an unlabelled series.

    Prometheus reads an absent label as "", which does not match "daniel-pi" — so an
    origin-less series survives. Pinned because the obvious alternative spelling, an
    `origin=~"daniel-box|daniel-server"` allowlist, silently drops them instead, and the
    difference only shows up on a Prometheus that applies no origin label at all.
    """
    sel = bridge_io.host_metric_sel()

    assert sel.startswith("{") and sel.endswith("}")
    assert "!~" in sel, "must be a negative match, not an origin allowlist"


def test_the_exclusion_is_rendered_in_the_env_secret():
    """The deployed value must exist, not be inherited from check.py's default.

    check.py's `_env` default and the env-secret are two places one fact can live, and only the
    env-secret is what actually runs. Rendering it explicitly is also what lets a maintenance
    window widen or clear the exclusion the way HOST_ORIGINS_MIN can be lowered.
    """
    from pathlib import Path

    env_secret = (
        Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    )
    text = env_secret.read_text()

    assert "HOST_METRIC_ORIGIN_EXCLUDE:" in text
    assert "LOG_ERROR_SELECTOR:" in text, (
        "the log-pattern arm's selector must be rendered too — it is the field most likely to "
        "need changing without a code edit, and a wrong one makes the arm silently inert"
    )


def test_the_exclusion_is_overridable_without_editing_the_file(monkeypatch):
    """An operator can widen or clear the exclusion from the env, like every other threshold."""
    monkeypatch.setattr(
        bridge_config, "HOST_METRIC_ORIGIN_EXCLUDE", "daniel-pi|daniel-spare"
    )
    assert 'origin!~"daniel-pi|daniel-spare"' in bridge_io.host_metric_sel()

    monkeypatch.setattr(bridge_config, "HOST_METRIC_ORIGIN_EXCLUDE", "")
    assert bridge_io.host_metric_sel() == "", (
        "cleared, the selector must vanish entirely rather than render an empty matcher"
    )
