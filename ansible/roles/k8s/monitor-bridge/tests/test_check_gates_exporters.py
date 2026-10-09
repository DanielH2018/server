"""The exporter-reachability gate: node-exporter and cadvisor as suppression sources.

A host or container metric read through a dead exporter returns an empty vector, which the
host checks cannot tell from a healthy one. The gate turns that into "cannot tell" for exactly
the checks the dead exporter feeds, and EXPORTER_DEPENDENT is the map from job to checks. Both
of its axes are guarded here — its values (are they real checks) and its keys (does every
scraped node-exporter job appear).
"""

import re

from dataclasses import replace

import pytest

import bridge.config
import bridge.net
import checks.cluster
import check
import gates
import registry
from _check_gate_helpers import mk
from _fake_sources import FakeSources
from bridge.types import Check
from gates import Gates

from lib.repo_paths import REPO as _REPO


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
            # cadvisor is not in EXPORTER_DEPENDENT — a down series under its old job name
            # must not trigger suppression of anything.
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
    assert gates.down_exporters(up) == expected


def test_exporter_dependent_values_are_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every suppressed dependent is a real check name, so the
    # exporter gate can't silently drift, and every dependent is also prom-dependent.
    names = {c.name for c in registry.build_checks()}
    for deps in gates.EXPORTER_DEPENDENT.values():
        assert deps <= names
        assert deps <= gates.PROM_DEPENDENT


# ── EXPORTER_DEPENDENT's KEYS, the axis the test above cannot cover ─────────────────────────
# The guard above checks the map's VALUES. A map carrying only `node` while daniel-pi scrapes
# under `node-pi` would leave the Pi's exporter death suppressing nothing and double-paging. A
# value guard structurally cannot see a missing key: the entries that ARE there stay correct,
# and the test reads green.
#
# Derived from the scrape config rather than transcribed, for the reason _promtail_relabel_targets
# gives in test_check_loki.py — a transcribed list cannot follow a rename. Renaming a scrape job
# would leave a literal here naming a job nothing emits, so the NEW name goes unmapped while the
# dead one passes: the guard reporting the opposite of the truth.
_PROM_SCRAPE_CONFIG = (
    _REPO / "ansible/roles/k8s/observability/templates/prometheus.yaml.j2"
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
    unmapped = _node_exporter_jobs() - set(gates.EXPORTER_DEPENDENT)
    assert not unmapped, (
        f"node-exporter scrape job(s) {sorted(unmapped)} have no EXPORTER_DEPENDENT entry, so a "
        "dead exporter there suppresses nothing. Decide which checks that job's hosts feed and add "
        "it — `host_temp` at minimum, plus disk/memory unless HOST_METRIC_ORIGIN_EXCLUDE excludes "
        "every origin the job declares."
    )


def test_a_job_whose_origins_are_all_excluded_suppresses_no_host_metric_check(cfg):
    """The other half of the same defect: the two axes have to agree.

    EXPORTER_DEPENDENT keys by job; check_disk and check_mem exclude by ORIGIN. A host added to one
    axis and not the other is exactly this defect. Only statically-labelled jobs are
    readable here — `node` discovers its origins from k8s at scrape time — so this covers `node-pi`.
    """
    excluded = re.compile(cfg.HOST_METRIC_ORIGIN_EXCLUDE)
    checked = 0
    for name, block in _scrape_job_blocks(_PROM_SCRAPE_CONFIG.read_text()):
        if name not in gates.EXPORTER_DEPENDENT:
            continue
        origins = re.findall(r"^\s+origin:\s*(\S+)", block, flags=re.M)
        if not origins or not all(excluded.fullmatch(o) for o in origins):
            continue
        checked += 1
        leaked = gates.EXPORTER_DEPENDENT[name] & {"disk", "memory"}
        assert not leaked, (
            f"job {name!r} declares only origins excluded by HOST_METRIC_ORIGIN_EXCLUDE "
            f"({cfg.HOST_METRIC_ORIGIN_EXCLUDE!r}), so check_disk and check_mem never read them "
            f"— suppressing {sorted(leaked)} there hides a real fault and reports nothing"
        )
    assert checked, "no excluded statically-labelled job found; this guard is inert"


def _wire_run_once_prom_up(cfg, monkeypatch, up_vector, checks, prom_dependent):
    """Drive run_once with Prometheus UP and a stated `up` vector; capture what ran + pushed.

    The production EXPORTER_DEPENDENT map is deliberately left in place — this suite is about
    which jobs it suppresses, so stating a sentinel map would test the driver rather than the
    table.
    """
    ran, pushes = [], []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, t, ok, m: pushes.append((t, ok, m))
    )
    check.run_once(
        cfg,
        FakeSources(prom_vector=lambda q: up_vector if q == "up" else []),
        [Check(n, "tok_%s" % n, mk(ran, n)) for n in checks],
        gates=Gates(
            prom_dependent=frozenset(prom_dependent),
            probe_prometheus=lambda _cfg, _src: (True, "prom ok"),
            probe_loki=lambda _cfg, _src: (True, "loki ok"),
            probe_wan=lambda _cfg, _src: (True, "wan ok"),
        ),
    )
    return ran, pushes


def test_run_once_suppresses_node_dependents_when_node_exporter_down(monkeypatch, cfg):
    up = [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 1.0)]
    ran, pushes = _wire_run_once_prom_up(
        cfg,
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


def _fake_vectors(cfg, by_query):
    """(cfg, src): a FakeSources whose prom_vector answers by substring of the query.

    Drops CADVISOR_PODS_MIN to 0 for its callers, which are all offender-logic tests built on
    one- or two-pod fixtures — far below the real floor. Scoped here rather than as an autouse
    fixture on purpose: an estate-wide default of 0 would make the coverage floor invisible to
    every other test in the suite, which is the failure the floor itself exists to prevent. The
    floor's own tests answer prom_vector directly and never come through here.
    """
    cfg = replace(cfg, CADVISOR_PODS_MIN=0)

    def fake(promql):
        for key, vec in by_query.items():
            if key in promql:
                return vec
        raise AssertionError("unexpected query: %s" % promql)

    return cfg, FakeSources(prom_vector=fake)


def test_check_restarts_names_the_looping_pod(cfg):
    cfg, src = _fake_vectors(
        cfg,
        {
            "container_start_time_seconds": [
                ({"pod": "n8n-abc"}, 7.0),
                ({"pod": "quiet"}, 0.0),
            ]
        },
    )
    ok, msg = checks.cluster.check_restarts(cfg, src)
    assert not ok and "n8n-abc" in msg


def test_check_restarts_quiet_is_up(cfg):
    cfg, src = _fake_vectors(
        cfg, {"container_start_time_seconds": [({"pod": "quiet"}, 1.0)]}
    )
    ok, _ = checks.cluster.check_restarts(cfg, src)
    assert ok


def test_check_oom_names_the_killed_pod(cfg):
    cfg, src = _fake_vectors(
        cfg, {"container_oom_events_total": [({"pod": "karakeep-x"}, 2.0)]}
    )
    ok, msg = checks.cluster.check_oom(cfg, src)
    assert not ok and "karakeep-x" in msg


def test_check_cpu_throttle_needs_both_gates_and_streak(cfg):
    # 90% throttled AND real cores lost — but only pages on the CPU_CONSECUTIVE-th
    # consecutive breaching cycle.
    checks.cluster._cpu_breach_streak = 0
    cfg, src = _fake_vectors(
        cfg,
        {
            "container_cpu_cfs_throttled_periods_total": [({"pod": "tdarr-y"}, 0.9)],
            "container_cpu_cfs_throttled_seconds_total": [({"pod": "tdarr-y"}, 0.5)],
        },
    )
    for _ in range(cfg.CPU_CONSECUTIVE - 1):
        ok, msg = checks.cluster.check_cpu_throttle(cfg, src)
        assert ok and "tdarr-y" in msg  # named but not paging yet
    ok, msg = checks.cluster.check_cpu_throttle(cfg, src)
    assert not ok and "tdarr-y" in msg
    checks.cluster._cpu_breach_streak = 0


def test_check_cpu_throttle_tiny_loss_stays_up(cfg):
    # High ratio but negligible absolute cores lost — the volume floor gates it out.
    checks.cluster._cpu_breach_streak = 0
    cfg, src = _fake_vectors(
        cfg,
        {
            "container_cpu_cfs_throttled_periods_total": [({"pod": "sidecar"}, 0.9)],
            "container_cpu_cfs_throttled_seconds_total": [({"pod": "sidecar"}, 0.0001)],
        },
    )
    ok, _ = checks.cluster.check_cpu_throttle(cfg, src)
    assert ok


def test_run_once_suppression_without_cadvisor_series(monkeypatch, cfg):
    # Only the node job exists in `up`.
    up = [({"job": "node"}, 0.0)]
    ran, _ = _wire_run_once_prom_up(
        cfg,
        monkeypatch,
        up,
        ["disk", "memory", "targets"],
        {"disk", "memory", "targets"},
    )
    assert not ({"disk", "memory"} & set(ran))
    assert "targets" in ran


def test_run_once_no_suppression_when_exporters_up(monkeypatch, cfg):
    up = [({"job": "node"}, 1.0)]
    ran, _ = _wire_run_once_prom_up(
        cfg, monkeypatch, up, ["disk", "memory"], {"disk", "memory"}
    )
    assert "disk" in ran and "memory" in ran


def test_run_once_up_probe_failure_does_not_suppress(monkeypatch, cfg):
    # If the `up` probe itself errors, fail toward alerting: run the checks, don't mask them.
    def boom(q):
        raise RuntimeError("prom hiccup")

    ran, pushes = [], []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, t, ok, m: pushes.append((t, ok, m))
    )
    check.run_once(
        cfg,
        FakeSources(prom_vector=boom),
        [Check("disk", "tok_disk", mk(ran, "disk"))],
        gates=Gates(
            prom_dependent=frozenset({"disk"}),
            probe_prometheus=lambda _cfg, _src: (True, "prom ok"),
            probe_loki=lambda _cfg, _src: (True, "loki ok"),
            probe_wan=lambda _cfg, _src: (True, "wan ok"),
        ),
    )
    assert "disk" in ran  # not suppressed


# ── The origin pin the stub above cannot see ────────────────────────────────────────────────
# `_wire_run_once_prom_up` answers the literal query "up" and nothing else, which is exactly how
# a Prometheus honouring a selector behaves. What hides the fault is the `cfg` fixture:
# PROM_ORIGIN is empty there, so the probe's `up%s % origin_sel(cfg)` rendered as a bare `up` and
# the pin the pod actually runs with never appeared in a test.

# One target per node-exporter, labelled the way Prometheus labels them: the `node` job relabels
# each target with its own node name, `node-pi` is statically labelled daniel-pi.
_THREE_ORIGIN_UP = [
    ({"job": "node", "origin": "daniel-box"}, 1.0),
    ({"job": "node", "origin": "daniel-server"}, 1.0),
    ({"job": "node-pi", "origin": "daniel-pi"}, 0.0),
]


def test_pi_exporter_death_suppresses_its_dependents_under_the_deployed_origin_pin(
    monkeypatch, cfg
):
    """The Pi's node-exporter is down; pi_pressure and host_temp must not also page.

    Unsuppressed, pi_pressure pages with "node-pi series missing load/mem/fs", which is
    exactly what EXPORTER_DEPENDENT["node-pi"] exists to suppress. Under
    the pinned probe the whole vector was invisible — daniel-server's only series is healthy, so
    `up{origin="daniel-server"}` carried no dead exporter to suppress on.
    """
    names = ["pi_pressure", "host_temp", "targets"]
    ran, pushes = _wire_run_once_prom_up(
        replace(cfg, PROM_ORIGIN='origin="daniel-server"'),
        monkeypatch,
        _THREE_ORIGIN_UP,
        names,
        names,
    )
    assert not ({"pi_pressure", "host_temp"} & set(ran))
    assert "targets" in ran  # Scrape Targets is still the single page
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    assert by_tok["tok_pi_pressure"][0] is True
    assert "exporter" in by_tok["tok_pi_pressure"][1].lower()


def test_an_origin_pinned_probe_would_see_no_dead_exporter():
    """The reject half, stated as the query it turns on rather than by reverting the fix.

    `down_exporters` reads whatever vector the probe returns. A pinned probe selects nothing
    from a fleet whose only dead exporter is on another host, so the gate suppresses nothing.
    """
    pinned = [(m, v) for m, v in _THREE_ORIGIN_UP if m["origin"] == "daniel-server"]
    assert gates.down_exporters(pinned) == set()
    assert gates.down_exporters(_THREE_ORIGIN_UP) == {"node-pi"}
