"""The exporter suppression: a dead node exporter skips the rows that read its series.

A metric read through a dead exporter returns an empty vector, which `host_packages` cannot tell
from a healthy one. `check.EXPORTER_DEPENDENT` maps each scrape job to the rows it feeds, and
`run_once` skips those rows while the job is down, so Scrape Targets is the single page. Both
axes of the map are guarded here, its values (are they real checks) and its keys (does every
scraped node-exporter job appear), and so is the probe that runs without a gate row.
"""

import re
from dataclasses import replace

import pytest

import check
import checks.host_packages
import registry
from _fake_sources import FakeExport, FakeSources, mk
from bridge.types import Check
from lib.repo_paths import REPO as _REPO


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
    assert check.down_exporters(up) == expected


def test_exporter_dependent_names_real_prometheus_gated_checks_and_is_not_empty():
    """The non-vacuity half is the load-bearing one: an emptied map is a subset of everything
    and would suppress nothing."""
    gated = {c.name for c in registry.build_checks() if c.gate == "prometheus"}
    dependents = set().union(*check.EXPORTER_DEPENDENT.values())
    assert {"host_packages"} <= dependents <= gated


# ── EXPORTER_DEPENDENT's KEYS, the axis the test above cannot cover ─────────────────────────
# A map carrying only `node` while daniel-pi scrapes under `node-pi` would leave the Pi's
# exporter death suppressing nothing and double-paging. A value guard structurally cannot see a
# missing key. Derived from the scrape config rather than transcribed, so a renamed job cannot
# leave a literal here naming a job nothing emits.
_PROM_SCRAPE_CONFIG = _REPO / "k8s/roles/observability/templates/prometheus.yaml.j2"
NODE_EXPORTER_PORT = "9100"


def _node_exporter_jobs():
    """Scrape job names whose target is node-exporter, identified by its port.

    Port rather than job name: `node` finds its targets by k8s SD on `app=node-exporter` while
    `node-pi` uses a static `<ip>:9100`, so the port is the only thing both spell out. Comments
    are stripped first, because a retirement note two blocks earlier mentions 9100.
    """
    body = "\n".join(
        line
        for line in _PROM_SCRAPE_CONFIG.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    blocks = re.split(r"^\s*- job_name:\s*", body, flags=re.M)[1:]
    return {
        b.partition("\n")[0].strip()
        for b in blocks
        if NODE_EXPORTER_PORT in b.partition("\n")[2]
    }


def test_the_scrape_config_is_actually_parseable():
    """A path typo or a reshaped config empties _node_exporter_jobs(), and the guard below then
    passes vacuously — the inert-check case this repo has paid for twice."""
    assert "node" in _node_exporter_jobs()


# The Pi's exporter feeds no bridge row since M6's batch B8 moved `pi_pressure` to the `hosts`
# rules and `PiPortDead`. Its one dependent alert is held by an Alertmanager inhibit on
# `PiHostDown` (alertmanager-secret.yaml.j2), so the map carries no `node-pi` key.
JOBS_WITH_NO_BRIDGE_DEPENDENTS = {"node-pi"}


def test_every_node_exporter_job_is_mapped_in_exporter_dependent():
    """The reject half: adding a node-exporter host fails here until its job is placed."""
    unmapped = (
        _node_exporter_jobs()
        - set(check.EXPORTER_DEPENDENT)
        - JOBS_WITH_NO_BRIDGE_DEPENDENTS
    )
    assert not unmapped, (
        f"node-exporter scrape job(s) {sorted(unmapped)} have no EXPORTER_DEPENDENT entry, so a "
        "dead exporter there suppresses nothing. Decide which checks that job's hosts feed and "
        "add it."
    )


def test_the_exempt_jobs_are_still_scraped_and_unmapped():
    assert JOBS_WITH_NO_BRIDGE_DEPENDENTS <= _node_exporter_jobs()
    assert not JOBS_WITH_NO_BRIDGE_DEPENDENTS & set(check.EXPORTER_DEPENDENT)


# ── run_once ─────────────────────────────────────────────────────────────────────────────────


def _cycle(cfg, up_vector, *, host_packages=None):
    """One cycle: `host_packages`, which sits under `node`, and `targets`, which sits under no job.

    The production EXPORTER_DEPENDENT map is left in place: this suite is about which jobs it
    skips, so stating a sentinel map would test the driver rather than the table. There is no
    `prometheus` gate row: Cutover-2 deleted it, and the probe runs because `host_packages` is
    enabled and under `node`, which is the wiring the first test below proves.

    Returns (ran, exported, src): the names whose body ran, {name: verdict}, and the fake.
    """
    ran = []
    export = FakeExport()
    src = FakeSources(prom_vector=lambda q: up_vector if q == "up" else [])
    check.run_once(
        cfg,
        src,
        [
            Check(
                "host_packages", host_packages or mk(ran, "host_packages"), "prometheus"
            ),
            Check("targets", mk(ran, "targets")),
        ],
        export=export,
    )
    return ran, export.verdicts, src


def test_a_dead_node_exporter_skips_its_dependents_and_scrape_targets_still_pages(
    cfg, capsys
):
    ran, exported, _ = _cycle(cfg, [({"job": "node"}, 0.0), ({"job": "cadvisor"}, 1.0)])
    assert "host_packages" not in ran
    assert "targets" in ran
    # A skipped row exports 0 without running its body, and the cycle counts it as skipped.
    assert exported == {"host_packages": False, "targets": True}
    assert "CYCLE 1 up, 0 down, 1 skipped" in capsys.readouterr().out


def test_the_exporter_probe_runs_without_a_prometheus_gate_row(cfg):
    """RED on a run loop that only probes after a `prometheus` row: no such row exists, so the
    dependents would run against an empty vector from a dead exporter.
    """
    ran, _, src = _cycle(cfg, [({"job": "node"}, 0.0)])
    assert ran == ["targets"]
    assert src.queries("prom_vector") == ["up"]


def test_no_exporter_probe_when_the_dependent_is_not_enabled(cfg):
    """A narrowed run that leaves `host_packages` out sends no `up` query."""
    ran = []
    src = FakeSources()
    check.run_once(
        replace(cfg, CHECKS_SKIP=frozenset({"host_packages"})),
        src,
        [Check("host_packages", mk(ran, "host_packages"), "prometheus")],
    )
    assert ran == []
    assert src.queries("prom_vector") == []


def test_no_suppression_when_the_exporters_are_up(cfg):
    ran, _, _ = _cycle(cfg, [({"job": "node"}, 1.0)])
    assert ran == ["host_packages", "targets"]


def test_an_up_probe_that_fails_does_not_suppress(cfg):
    """If the `up` probe itself errors, fail toward alerting: run the checks, don't mask them."""

    def boom(q):
        raise RuntimeError("prom hiccup")

    ran = []
    check.run_once(
        cfg,
        FakeSources(prom_vector=boom),
        [Check("host_packages", mk(ran, "host_packages"), "prometheus")],
    )
    assert ran == ["host_packages"]


# The probe is a bare `up`, not pinned to one `origin`. One target per node-exporter, labelled
# the way Prometheus labels them: the `node` job relabels each target with its own node name,
# `node-pi` is statically labelled daniel-pi.
_THREE_ORIGIN_UP = [
    ({"job": "node", "origin": "daniel-box"}, 0.0),
    ({"job": "node", "origin": "daniel-server"}, 1.0),
    ({"job": "node-pi", "origin": "daniel-pi"}, 1.0),
]


def test_a_dead_node_exporter_on_another_origin_suppresses_its_dependents(cfg):
    """daniel-box's node-exporter is down; host_packages must not also page.

    Under a pinned probe the whole vector was invisible: daniel-server's only series is
    healthy, so `up{origin="daniel-server"}` carried no dead exporter to suppress on.
    """
    ran, exported, _ = _cycle(cfg, _THREE_ORIGIN_UP)
    assert "host_packages" not in ran
    assert "targets" in ran
    assert exported["host_packages"] is False


def test_an_origin_pinned_probe_would_see_no_dead_exporter():
    """The reject half, stated as the query it turns on rather than by reverting the fix."""
    pinned = [(m, v) for m, v in _THREE_ORIGIN_UP if m["origin"] == "daniel-server"]
    assert check.down_exporters(pinned) == set()
    assert check.down_exporters(_THREE_ORIGIN_UP) == {"node"}


def test_a_dead_pi_exporter_suppresses_nothing_in_the_bridge():
    """The exemption's accepting half: no bridge row reads `node-pi` series, so none is held."""
    assert (
        check.down_exporters([({"job": "node-pi", "origin": "daniel-pi"}, 0.0)])
        == set()
    )


@pytest.mark.parametrize(
    ("node_up", "line"),
    [(0.0, None), (1.0, "fetch series absent")],
    ids=["suppressed_while_the_exporter_is_down", "pages_once_it_is_up"],
)
def test_the_real_host_packages_body_under_the_suppression(cfg, capsys, node_up, line):
    """With the builder series absent, the row is skipped while the exporter that publishes them
    is dead, and its body reports DOWN on the absent series once the exporter is up.
    """
    sources = FakeSources(
        prom_vector=lambda q: [({"job": "node"}, node_up)] if q == "up" else [],
        prom_scalar=lambda q: None,
    )
    export = FakeExport()
    check.run_once(
        cfg,
        sources,
        [
            Check(
                "host_packages", checks.host_packages.check_host_packages, "prometheus"
            )
        ],
        export=export,
    )
    out = capsys.readouterr().out
    assert export.verdicts == {"host_packages": False}
    if line is None:
        assert "DOWN host_packages" not in out
        assert "1 skipped" in out
    else:
        assert "DOWN host_packages" in out
        assert line in out.lower()
