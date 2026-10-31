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

import bridge.net
from bridge.config import load_config


def test_origin_sel_is_empty_without_a_pin(monkeypatch, cfg):
    # A Prometheus with no `origin` label (external_labels apply on remote-write and never to
    # local storage) would select NOTHING under a pin and read as healthy. Empty must stay
    # empty.
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

    The pin is a constant rather than derived from the Prometheus URL, because a derivation
    with one reachable branch is a constant. Deriving it from the URL's SHAPE would answer
    "does this look in-cluster" when the question is "does this Prometheus carry `origin`".

    Stated to load_config as an environment rather than reached by reloading the module: a
    reload re-runs one module against the real os.environ, so the test would have to mutate the
    process to ask its question and undo the mutation afterwards.
    """
    assert (
        load_config({"PROMETHEUS_URL": "https://prom-k8s.example"}).PROM_ORIGIN
        == 'origin="daniel-server"'
    )
    # An unset PROMETHEUS_URL pins too. It is a config fault main() exits 2 on, but leaving the
    # pin off here would make "configured nothing" the one silently-unpinned configuration.
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
