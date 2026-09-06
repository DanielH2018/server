"""`probe.py metric`: saying when a range window outruns Prometheus' retention.

This Prometheus holds ~11.5 days. A `[30d]` selector returns those eleven days with no error,
no warning and no partial-data marker, so a ratio derived from it quotes a denominator nearly
three times the covered one — issue #1314, and issue #1186's table before it. The warning is
the thing under test; the value itself is unchanged.
"""

from diagnostics.probe_lib import cli_parser
from diagnostics.probe_lib import core
from diagnostics.probe_lib import metrics

# 11.5 days, the measured retention on daniel-box.
COVERED_S = 11.5 * 86400


def _fake_prometheus(monkeypatch, covered_s=COVERED_S, span_body=None):
    """Patch the network out and return the list of urls fetched, in order."""
    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        if "prometheus_tsdb_lowest_timestamp_seconds" in url:
            if span_body is not None:
                return span_body
            return (
                '{"status":"success","data":{"resultType":"vector","result":'
                '[{"metric":{},"value":[1788691702.874,"%s"]}]}}' % covered_s
            )
        return '{"data":{"resultType":"vector","result":[]}}'

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


def _run(monkeypatch, promql, **kwargs):
    _fake_prometheus(monkeypatch, **kwargs)
    ns = cli_parser._build_parser().parse_args(["metric", promql])
    return metrics.run_query(ns)


def test_requested_range_reads_every_selector_form():
    assert metrics.requested_range_seconds("up") is None
    assert metrics.requested_range_seconds("count_over_time(up[30d])") == 30 * 86400
    assert metrics.requested_range_seconds("rate(x[500ms])") == 0.5
    assert metrics.requested_range_seconds("rate(x[1h30m])") == 5400
    # A subquery's `:<resolution>` tail is the step, not part of the window.
    assert metrics.requested_range_seconds("max_over_time(rate(x[5m])[30d:1h])") == (
        30 * 86400
    )
    # `offset` shifts the window; the width is still what retention has to cover.
    assert metrics.requested_range_seconds("count_over_time(up[7d] offset 1d)") == (
        7 * 86400
    )
    # Several selectors: the widest is the one that gets truncated first.
    assert metrics.requested_range_seconds("sum(rate(a[5m])) + sum(rate(b[90d]))") == (
        90 * 86400
    )


def test_a_window_within_retention_is_clean(monkeypatch, capsys):
    assert _run(monkeypatch, "count_over_time(up[1h])") == 0
    assert capsys.readouterr().err == ""


def test_an_instant_query_makes_no_span_probe(monkeypatch, capsys):
    seen = _fake_prometheus(monkeypatch)
    ns = cli_parser._build_parser().parse_args(["metric", "up"])
    assert metrics.run_query(ns) == 0
    assert len(seen) == 1
    assert capsys.readouterr().err == ""


def test_a_window_past_retention_is_flagged(monkeypatch, capsys):
    assert _run(monkeypatch, "count_over_time(up[30d])") == 0
    err = capsys.readouterr().err
    assert "covered span 11.5d of 30d requested" in err


def test_the_value_still_prints_and_stays_on_stdout(monkeypatch, capsys):
    assert _run(monkeypatch, "count_over_time(up[30d])") == 0
    out = capsys.readouterr()
    assert out.out.strip() == "no data"
    assert "covered span" not in out.out


def test_an_unreadable_span_stays_silent(monkeypatch, capsys):
    # The probe is an annotation. A Prometheus that cannot answer it must not fail the query.
    assert _run(monkeypatch, "count_over_time(up[30d])", span_body="not json") == 0
    assert capsys.readouterr().err == ""


def test_an_empty_tsdb_sentinel_is_not_read_as_a_span(monkeypatch, capsys):
    # An empty TSDB reports a max-int lowest timestamp, so the subtraction lands negative.
    assert _run(monkeypatch, "count_over_time(up[30d])", covered_s=-9.2e15) == 0
    assert capsys.readouterr().err == ""


def test_the_span_probe_goes_through_the_query_path(monkeypatch):
    # The prometheus IngressRoute admits `/api/v1/query` and `/api/v1/targets` only, so a
    # probe reaching for `/api/v1/status/tsdb` would 404 in production and pass here.
    seen = _fake_prometheus(monkeypatch)
    ns = cli_parser._build_parser().parse_args(["metric", "count_over_time(up[30d])"])
    metrics.run_query(ns)
    span_urls = [u for u in seen if "lowest_timestamp" in u]
    assert len(span_urls) == 1
    assert "/api/v1/query?" in span_urls[0]


def test_format_span_reads_as_a_selector_does():
    assert metrics.format_span(11.5 * 86400) == "11.5d"
    assert metrics.format_span(30 * 86400) == "30d"
    assert metrics.format_span(3600 * 3.25) == "3.2h"
    assert metrics.format_span(45 * 60) == "45m"
    assert metrics.format_span(30) == "30s"
