"""check_traefik_404_flood: the arm that would have caught #1322's total-404 edge.

Its two neighbours (check_traefik_5xx, check_traefik_latency) logged `0 service(s) above
floor` for 3.5 hours while every HTTPS route 404'd, because a router-less edge emits no
`traefik_service_*` series at all and their per-service loops read the empty vector as
healthy. Each pair below is one input this check must accept and one it must reject, so a
rule that silently stopped matching fails its own test rather than reading green.
"""

from dataclasses import replace

import bridge.net
import checks.cluster


def _prom(monkeypatch, total, notfound):
    """Answer the check's two queries by which one it is asking, not by call order.

    Keyed on the `code="404"` selector rather than on a two-element side_effect: the check
    short-circuits before the second query on both absent-data branches, so an order-keyed
    stub would hand the numerator's answer to the denominator the moment a branch moved.
    """

    def _scalar(_cfg, promql, *a, **k):
        return notfound if 'code="404"' in promql else total

    monkeypatch.setattr(bridge.net, "prom_scalar", _scalar)


def test_ordinary_404_trickle_is_clean(monkeypatch, cfg):
    # The live shape, measured 2026-09-06: 0.033 of 0.833 rps is 4.0% 404s — favicons, probes
    # and stale bookmarks. This is the common case and must stay green, or the arm is ignored.
    _prom(monkeypatch, 0.8333333333333334, 0.03333333333333333)
    ok, msg = checks.cluster.check_traefik_404_flood(cfg)
    assert ok, msg
    assert "4.0%" in msg


def test_total_404_edge_is_flagged(monkeypatch, cfg):
    # The #1322 window exactly: `sum by (code)(rate(traefik_entrypoint_requests_total[10m]
    # offset 3h))` returned code=404 = 0.61 and nothing else.
    _prom(monkeypatch, 0.61, 0.61)
    ok, msg = checks.cluster.check_traefik_404_flood(cfg)
    assert not ok
    assert "100%" in msg
    assert "0.61 rps" in msg


def test_a_quiet_edge_below_the_rps_floor_is_clean(monkeypatch, cfg):
    # One 404 on a near-idle edge is not a 100%-404 alarm — the same floor the 5xx check uses.
    _prom(monkeypatch, 0.01, 0.01)
    ok, msg = checks.cluster.check_traefik_404_flood(cfg)
    assert ok, msg
    assert "below the" in msg


def test_an_unscraped_traefik_is_clean_and_says_so(monkeypatch, cfg):
    # Absent series means the exporter is blind, which is Scrape Targets' page. Paging here
    # too would be two alerts for one root cause — the rule gates.py applies everywhere else.
    _prom(monkeypatch, None, None)
    ok, msg = checks.cluster.check_traefik_404_flood(cfg)
    assert ok, msg
    assert "Scrape Targets" in msg


def test_no_404_series_reads_as_zero_rather_than_unknown(monkeypatch, cfg):
    # A 404 rate with no series is a genuine zero: traffic is flowing and none of it 404'd.
    _prom(monkeypatch, 0.5, None)
    ok, msg = checks.cluster.check_traefik_404_flood(cfg)
    assert ok, msg
    assert "0.0%" in msg


def test_the_threshold_is_the_knob_that_decides(monkeypatch, cfg):
    # Pins TRAEFIK_404_PCT to the verdict rather than to a default: at 50% the same ratio that
    # passes under 90 must fail. Without this a threshold read from the wrong config field
    # would keep both pairs above green.
    _prom(monkeypatch, 1.0, 0.6)
    assert checks.cluster.check_traefik_404_flood(cfg)[0]
    assert not checks.cluster.check_traefik_404_flood(
        replace(cfg, TRAEFIK_404_PCT=50.0)
    )[0]
