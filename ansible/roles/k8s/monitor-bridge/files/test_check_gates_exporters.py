"""The exporter-reachability gate: node-exporter and cadvisor as suppression sources.

A host or container metric read through a dead exporter returns an empty vector, which the
host checks cannot tell from a healthy one. The gate turns that into "cannot tell" for exactly
the checks the dead exporter feeds, and EXPORTER_DEPENDENT is the map from job to checks. Both
of its axes are guarded here — its values (are they real checks) and its keys (does every
scraped node-exporter job appear).
"""

import re
from pathlib import Path

import pytest

import bridge_config
import bridge_io
import check

_REPO = Path(__file__).resolve().parents[5]


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


# ── EXPORTER_DEPENDENT's KEYS, the axis the test above cannot cover ─────────────────────────
# The guard above checks the map's VALUES. Its keys went unchecked until 2026-08-29, and the map
# shipped carrying only `node` while daniel-pi scrapes under `node-pi` — so the Pi's exporter death
# suppressed nothing and double-paged. A value guard structurally cannot see a missing key: the
# entries that ARE there stay correct, and the test reads green.
#
# Derived from the scrape config rather than transcribed, for the reason _promtail_relabel_targets
# gives in test_check_loki.py — a transcribed list cannot follow a rename. Renaming a scrape job
# would leave a literal here naming a job nothing emits, so the NEW name goes unmapped while the
# dead one passes: the guard reporting the opposite of the truth.
_PROM_SCRAPE_CONFIG = (
    _REPO / "ansible/roles/k8s/claude-otel/templates/prometheus.yaml.j2"
)
NODE_EXPORTER_PORT = "9100"


def _scrape_job_blocks(text):
    """(job_name, body) for each `- job_name:` block, in file order."""
    blocks = re.split(r"^\s*- job_name:\s*", text, flags=re.M)[1:]
    return [(b.partition("\n")[0].strip(), b.partition("\n")[2]) for b in blocks]


def _node_exporter_jobs():
    """Scrape job names whose target is node-exporter, identified by its port.

    Port rather than job name: `node` finds its targets by k8s SD on `app=node-exporter` while
    `node-pi` uses a static `<ip>:9100`, so the port is the only thing both spell out. Comments are
    stripped first — a retirement note two blocks earlier mentions 9100 and would otherwise
    attribute the port to `traefik-k8s`.
    """
    body = "\n".join(
        line
        for line in _PROM_SCRAPE_CONFIG.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    return {
        name for name, block in _scrape_job_blocks(body) if NODE_EXPORTER_PORT in block
    }


def test_the_scrape_config_is_actually_parseable():
    """A path typo or a reshaped config empties _node_exporter_jobs(), and the guard below then
    passes vacuously — the inert-check case this repo has paid for twice."""
    jobs = _node_exporter_jobs()
    assert "node" in jobs, (
        f"no node-exporter scrape jobs parsed from {_PROM_SCRAPE_CONFIG.name} (got {jobs!r}) — "
        "the path or the config shape changed, and the key guard below is now inert"
    )


def test_every_node_exporter_job_is_mapped_in_exporter_dependent():
    """The reject half: adding a node-exporter host fails here until its job is placed.

    Every node-exporter job carries hwmon series, so losing a host from any of them trips
    HWMON_TEMP_ORIGINS_MIN. The map is keyed by Prometheus `job`, so an unmapped job means one root
    cause pages twice — Scrape Targets plus a coverage complaint naming the same host.
    """
    unmapped = _node_exporter_jobs() - set(check.EXPORTER_DEPENDENT)
    assert not unmapped, (
        f"node-exporter scrape job(s) {sorted(unmapped)} have no EXPORTER_DEPENDENT entry, so a "
        "dead exporter there suppresses nothing. Decide which checks that job's hosts feed and add "
        "it — `host_temp` at minimum, plus disk/memory unless HOST_METRIC_ORIGIN_EXCLUDE excludes "
        "every origin the job declares."
    )


def test_a_job_whose_origins_are_all_excluded_suppresses_no_host_metric_check():
    """The other half of the same defect: the two axes have to agree.

    EXPORTER_DEPENDENT keys by job; check_disk and check_mem exclude by ORIGIN. A host added to one
    axis and not the other is exactly what shipped on 2026-08-29. Only statically-labelled jobs are
    readable here — `node` discovers its origins from k8s at scrape time — so this covers `node-pi`.
    """
    excluded = re.compile(bridge_config.HOST_METRIC_ORIGIN_EXCLUDE)
    checked = 0
    for name, block in _scrape_job_blocks(_PROM_SCRAPE_CONFIG.read_text()):
        if name not in check.EXPORTER_DEPENDENT:
            continue
        origins = re.findall(r"^\s+origin:\s*(\S+)", block, flags=re.M)
        if not origins or not all(excluded.fullmatch(o) for o in origins):
            continue
        checked += 1
        leaked = check.EXPORTER_DEPENDENT[name] & {"disk", "memory"}
        assert not leaked, (
            f"job {name!r} declares only origins excluded by HOST_METRIC_ORIGIN_EXCLUDE "
            f"({bridge_config.HOST_METRIC_ORIGIN_EXCLUDE!r}), so check_disk and check_mem never read them "
            f"— suppressing {sorted(leaked)} there hides a real fault and reports nothing"
        )
    assert checked, "no excluded statically-labelled job found; this guard is inert"


def _wire_run_once_prom_up(monkeypatch, up_vector, checks, prom_dependent):
    """Drive run_once with Prometheus UP and a stubbed `up` vector; capture what ran + pushed."""
    ran, pushes = [], []
    monkeypatch.setattr(bridge_io, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(
        bridge_io, "prom_vector", lambda q: up_vector if q == "up" else []
    )
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
    monkeypatch.setattr(bridge_config, "CADVISOR_PODS_MIN", 0)

    def fake(promql):
        for key, vec in by_query.items():
            if key in promql:
                return vec
        raise AssertionError("unexpected query: %s" % promql)

    monkeypatch.setattr(bridge_io, "prom_vector", fake)


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
    for _ in range(bridge_config.CPU_CONSECUTIVE - 1):
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
    monkeypatch.setattr(bridge_io, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(bridge_io, "prom_vector", boom)
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
