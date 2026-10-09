"""check_traefik_421: the arm that catches wedged clients.

A client whose connection SNICheck pinned to the wrong TLS options gets 421 at the router for
the life of that connection. The request never reaches a service, so the 5xx and latency
checks have nothing to read, and the 404 flood check counts a different code. Each pair below
is one input the check must accept and one it must reject.
"""

from dataclasses import replace

import checks.cluster_traefik
from _fake_sources import FakeSources

AUTHELIA = "homelab-authelia-5ac9e6c654eeb5a277d9@kubernetescrd"
LOKI_PUSH = "homelab-loki-homelab-push-monitoring-95752c50a917d4491bde@kubernetescrd"


def _sources(rates):
    """Sources answering the per-router 421 query with `rates`, a router -> rps mapping."""

    def _vector(promql, *a, **k):
        assert 'code="421"' in promql and "by (router)" in promql, promql
        return [({"router": r}, v) for r, v in rates.items()]

    return FakeSources(prom_vector=_vector)


def _cycles(cfg, rates, n):
    src = _sources(rates)
    return [checks.cluster_traefik.check_traefik_421(cfg, src) for _ in range(n)]


def test_a_one_shot_handshake_mismatch_is_clean(cfg):
    # A 421 outside a wedge: one window on uptime-kuma at
    # 0.0083 rps. Below the threshold, so it never starts a streak.
    rates = {"homelab-uptime-kuma-18c8147211c6d20211f0@kubernetescrd": 0.0083}
    for ok, msg in _cycles(cfg, rates, 5):
        assert ok, msg
        assert "421 ok" in msg


def test_a_wedged_router_pages_on_the_consecutive_cycle(cfg):
    # A wedge's shape: Loki push at 0.18 rps and authelia at a steady 0.1 rps.
    results = _cycles(
        cfg, {AUTHELIA: 0.1, LOKI_PUSH: 0.18}, cfg.TRAEFIK_421_CONSECUTIVE
    )
    for ok, msg in results[:-1]:
        assert ok, msg
        assert "down streak" in msg
    ok, msg = results[-1]
    assert not ok
    # Worst router first, and both named: the operator restarts the client behind each.
    assert msg.index(LOKI_PUSH) < msg.index(AUTHELIA)
    assert "0.18 rps" in msg and "0.10 rps" in msg


def test_a_clean_cycle_resets_the_streak(cfg):
    # A burst that clears before the Nth cycle must not carry its count into the next burst,
    # or two unrelated blips an hour apart would add up to a page.
    n = cfg.TRAEFIK_421_CONSECUTIVE
    _cycles(cfg, {AUTHELIA: 0.1}, n - 1)
    assert _cycles(cfg, {}, 1)[0][0]
    assert all(ok for ok, _ in _cycles(cfg, {AUTHELIA: 0.1}, n - 1))


def test_the_threshold_is_the_knob_that_decides(cfg):
    # Pins TRAEFIK_421_RPS to the verdict rather than to a default: the wedge rate that pages
    # under 0.02 must stay up once the threshold is raised past it.
    n = cfg.TRAEFIK_421_CONSECUTIVE
    assert not _cycles(cfg, {AUTHELIA: 0.1}, n)[-1][0]
    raised = replace(cfg, TRAEFIK_421_RPS=0.2)
    assert all(ok for ok, _ in _cycles(raised, {AUTHELIA: 0.1}, n + 1))
