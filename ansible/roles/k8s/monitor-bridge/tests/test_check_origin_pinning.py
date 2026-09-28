"""Where the `origin` pin may be applied, and where applying it selects nothing.

`origin` is set by ONE Prometheus relabel rule, on the `node` job. So `up` carries it and
cAdvisor series never do: pinning a cAdvisor query selects the empty vector, which every check
reading it decodes as healthy. `PROM_ORIGIN` therefore defaults to the pin and is turned off
only by stating `PROM_ORIGIN=""`, which is the escape hatch a Prometheus whose series carry no
`origin` would need.

These are source-level and selector-level guards rather than run-loop ones; the suppression
behaviour is `test_check_gates.py`.
"""

from dataclasses import replace
from pathlib import Path

import bridge.net
import check
from bridge.config import load_config


def test_origin_sel_is_empty_without_a_pin(monkeypatch, cfg):
    # Against the Docker Prometheus there is no `origin` label at all — external_labels apply on
    # remote-write and never to local storage — so a pin there would select NOTHING and read as
    # healthy. Empty must stay empty.
    cfg = replace(cfg, PROM_ORIGIN="")
    assert bridge.net.origin_sel(cfg) == ""
    assert bridge.net.origin_sel(cfg, 'name!=""') == '{name!=""}'


def test_origin_sel_appends_the_pin(monkeypatch, cfg):
    cfg = replace(cfg, PROM_ORIGIN='origin="daniel-server"')
    assert bridge.net.origin_sel(cfg) == '{origin="daniel-server"}'
    assert (
        bridge.net.origin_sel(cfg, 'name!=""') == '{name!="", origin="daniel-server"}'
    )


def test_the_pin_is_on_by_default():
    """THE default this guards. Every deployed check reads the cluster Prometheus, whose `node`
    job carries `origin` — so an unpinned disk/memory/targets query silently widens past the
    host it is meant to describe.

    Until #2825 this was derived from PROMETHEUS_URL and CLUSTER_PROMETHEUS_URL naming one
    instance. The second URL is gone, and a derivation with one reachable branch is a constant.
    Re-deriving it from the URL's SHAPE instead would answer "does this look in-cluster" when
    the question is "does this Prometheus carry `origin`".

    Stated to load_config as an environment rather than reached by reloading the module: a
    reload re-runs one module against the real os.environ, so the test would have to mutate the
    process to ask its question and undo the mutation afterwards.
    """
    assert (
        load_config({"PROMETHEUS_URL": "https://prom-k8s.example"}).PROM_ORIGIN
        == 'origin="daniel-server"'
    )
    # An unset PROMETHEUS_URL pins too: the code default names a Prometheus, so leaving the pin
    # off here would make "configured nothing" the one silently-unpinned configuration.
    assert load_config({}).PROM_ORIGIN == 'origin="daniel-server"'


def test_an_explicit_empty_pin_wins():
    """The escape hatch, and the half that can go red on its own.

    A Prometheus whose series carry no `origin` label needs the pin OFF — pinning there selects
    the empty vector and every check reading it reports green forever. With the derivation gone
    this override is the only way to reach that state, so it is the branch that must keep
    working.
    """
    derived = load_config(
        {"PROMETHEUS_URL": "http://prometheus:9090", "PROM_ORIGIN": ""}
    )
    assert derived.PROM_ORIGIN == ""


_CADVISOR_METRICS = (
    "container_start_time_seconds",
    "container_oom_events_total",
    "container_cpu_cfs_throttled_periods_total",
    "container_cpu_cfs_periods_total",
    "container_cpu_cfs_throttled_seconds_total",
)


def test_cadvisor_checks_never_pin_the_origin(cfg):
    # REPLACES test_dual_estate_checks_all_pin_the_origin, which asserted the exact opposite and
    # was wrong from the Phase G retarget until 2026-08-24. Its premise — that these metrics
    # "genuinely exist in BOTH estates" — died with the Docker cAdvisor on 2026-08-14. `origin` is
    # set by ONE relabel rule, on the `node` job, so cAdvisor series never carry it; pinning them
    # selects the empty vector and check_restarts/check_oom/check_cpu report green forever.
    #
    # The old test enforced that bug rather than catching it, which is why the fix had to amend a
    # green test rather than a red one. Keep this assertion pointed at the SOURCE of the pin.
    # Every runtime module, not just the run loop: the checks are moving out by domain, and a
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
    if cfg.PROM_ORIGIN:
        assert cfg.PROM_ORIGIN not in bridge.net.cadvisor_sel('container!=""'), (
            "cadvisor_sel() must not apply the origin pin — cAdvisor series carry no origin label"
        )
    # Independent of the environment: the pin can only enter through PROM_ORIGIN, so a
    # cadvisor_sel() built with a sentinel pin must still come back without it.
    pinned = replace(cfg, PROM_ORIGIN='origin="sentinel"')
    assert "sentinel" not in bridge.net.cadvisor_sel('container!=""')
    assert "sentinel" in bridge.net.origin_sel(pinned, 'container!=""')


def test_up_still_pins_the_origin_where_the_label_exists(cfg):
    # The other half of the contract: `up` DOES carry origin (the node job is relabelled), and
    # targets_verdict depends on the pin to scope its floor to one estate. Dropping it there would
    # make check_targets a duplicate of check_cluster_targets and orphan daniel-server's
    # node-exporter, which is why the fix deliberately left this call site alone.
    if cfg.PROM_ORIGIN:
        assert cfg.PROM_ORIGIN in bridge.net.origin_sel(cfg), (
            "origin_sel() must still apply the pin when PROM_ORIGIN is set"
        )
