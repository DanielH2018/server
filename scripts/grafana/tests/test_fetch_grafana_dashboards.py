#!/usr/bin/env python3
"""Tests for fetch_grafana_dashboards' Prometheus lookup.

`prom_label_values` is how every query-type template variable gets a working default. It
reached Prometheus through `docker exec grafana` until 2026-09-10, which raised
FileNotFoundError on both cluster nodes — neither has Docker since the k3s migration. These
pin the replacement to the HTTP seam the rest of scripts/ uses.

Run: uv run pytest scripts/grafana/tests/test_fetch_grafana_dashboards.py
"""

import fetch_grafana_dashboards as fg

ENDPOINT = ("https://prometheus.example", "prometheus.example:443:10.0.0.240")


def _answering(label, values, seen):
    """A `series` seam that records the PromQL it was handed."""

    def series(promql):
        seen.append(promql)
        return [{"metric": {label: v}} for v in values]

    return series


def _refusing(promql):
    raise AssertionError("prom_label_values queried Prometheus when it should not have")


def test_a_label_values_query_returns_the_labels_prometheus_answered():
    seen = []
    values = fg.prom_label_values(
        "label_values(up, instance)",
        {},
        series=_answering("instance", ["b", "a", "b"], seen),
    )
    assert values == ["a", "b"]
    assert seen == ["group by (instance)(up)"]


def test_a_query_that_is_not_label_values_yields_no_default_and_queries_nothing():
    assert fg.prom_label_values("sum(rate(up[5m]))", {}, series=_refusing) == []


def test_a_chained_variable_is_substituted_before_the_query_is_built():
    seen = []
    fg.prom_label_values(
        'label_values(node_uname_info{job="$job"}, nodename)',
        {"job": "node"},
        series=_answering("nodename", ["daniel-box"], seen),
    )
    assert seen == ['group by (nodename)(node_uname_info{job="node"})']


def test_prom_series_asks_the_pinned_cluster_endpoint():
    seen = {}

    def fetch_json(url, resolve=None):
        seen["url"], seen["resolve"] = url, resolve
        return {"data": {"result": [{"metric": {"instance": "a"}}]}}, None

    result = fg.prom_series(
        "group by (instance)(up)", endpoint=lambda: ENDPOINT, fetch_json=fetch_json
    )
    assert result == [{"metric": {"instance": "a"}}]
    assert (
        seen["url"]
        == "https://prometheus.example/api/v1/query?query=group+by+%28instance%29%28up%29"
    )
    assert seen["resolve"] == ENDPOINT[1]


def test_prom_series_yields_no_series_when_prometheus_cannot_be_reached():
    result = fg.prom_series(
        "group by (instance)(up)",
        endpoint=lambda: ENDPOINT,
        fetch_json=lambda url, resolve=None: (None, 1),
    )
    assert result == []
