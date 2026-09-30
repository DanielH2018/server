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


def test_adapt_writes_the_same_form_as_the_exporter():
    """One serialisation for both writers, so neither churns the other's files: keys sorted at
    every depth, non-ASCII left literal (export_grafana_dashboards.dump is the reference)."""
    s, _ = fg.adapt(
        "none",
        {"uid": "x", "templating": {"list": []}, "panels": [{"title": "é", "id": 1}]},
    )
    assert s == (
        '{\n  "id": null,\n  "panels": [\n    {\n      "id": 1,\n      "title": "é"\n    }\n  ],\n'
        '  "templating": {\n    "list": []\n  },\n  "uid": "x"\n}'
    )


# Revision pinning (#2858). `revisions/latest` made a re-fetch return whatever grafana.com had
# published since, so an unrelated re-run could rewrite 13,746 lines of node-exporter-full.json.
# The named members keep the census non-vacuous: a renamed key would otherwise leave the
# `all(...)` below iterating an empty dict and passing.
PINNED_BOARDS = frozenset({"node-exporter-full"})


def test_every_vendored_dashboard_pins_a_revision():
    assert PINNED_BOARDS <= set(fg.DASHBOARDS)
    for name, pin in fg.DASHBOARDS.items():
        gnet_id, revision = pin
        assert isinstance(gnet_id, int), name
        assert isinstance(revision, int), name


def test_the_download_url_names_the_pinned_revision_and_never_latest():
    seen = []

    class _Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout=None):
        seen.append(url)
        return _Response()

    original = fg.urllib.request.urlopen
    fg.urllib.request.urlopen = urlopen
    try:
        fg.fetch(1860, 45)
    finally:
        fg.urllib.request.urlopen = original
    assert seen == ["https://grafana.com/api/dashboards/1860/revisions/45/download"]
    assert "latest" not in seen[0]


# Seeding, not refreshing (#2912). The committed boards carry hand edits the script does not
# reproduce, so a fresh fetch that differs from them must be refused rather than written. The
# stub boards carry no query variables, so `adapt` never reaches Prometheus. Two boards rather
# than the one the repo ships, because the all-or-nothing rule is about a refusal on one board
# stopping the write of another.
BOARDS = {"node-exporter-full": (1860, 45), "second": (14282, 1)}


def _board(title):
    return {"uid": title, "templating": {"list": []}, "panels": [{"title": title}]}


def _fetched(gnet_id, revision):
    return _board("upstream-%d" % gnet_id)


def _paths(outdir):
    return {
        name: outdir / fg.SUBDIR.get(name, "") / ("%s.json" % name) for name in BOARDS
    }


def _fresh_text(name):
    gnet_id, revision = BOARDS[name]
    s, _ = fg.adapt(name, _fetched(gnet_id, revision))
    return s + "\n"


def test_a_board_whose_committed_form_differs_is_refused_and_left_untouched(
    tmp_path, capsys
):
    paths = _paths(tmp_path)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("hand-edited\n", encoding="utf-8")

    assert fg.main([], fetch=_fetched, outdir=tmp_path, boards=BOARDS) == 1

    assert {p.read_text(encoding="utf-8") for p in paths.values()} == {"hand-edited\n"}
    err = capsys.readouterr().err
    assert "refusing to write" in err
    assert "second.json" in err and "--overwrite" in err


def test_a_refusal_writes_nothing_even_for_a_missing_board(tmp_path):
    paths = _paths(tmp_path)
    differing = paths["second"]
    differing.parent.mkdir(parents=True, exist_ok=True)
    differing.write_text("hand-edited\n", encoding="utf-8")

    assert fg.main([], fetch=_fetched, outdir=tmp_path, boards=BOARDS) == 1

    assert not paths["node-exporter-full"].exists()


def test_overwrite_takes_the_fetched_form_over_a_differing_board(tmp_path):
    paths = _paths(tmp_path)
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("hand-edited\n", encoding="utf-8")

    assert fg.main(["--overwrite"], fetch=_fetched, outdir=tmp_path, boards=BOARDS) == 0

    for name, path in paths.items():
        assert path.read_text(encoding="utf-8") == _fresh_text(name)


def test_a_missing_board_is_seeded_and_a_matching_one_is_not_rewritten(tmp_path):
    paths = _paths(tmp_path)
    matching = paths["second"]
    matching.parent.mkdir(parents=True, exist_ok=True)
    matching.write_text(_fresh_text("second"), encoding="utf-8")
    matching.chmod(0o444)  # a write to the matching board would raise PermissionError

    assert fg.main([], fetch=_fetched, outdir=tmp_path, boards=BOARDS) == 0

    assert paths["node-exporter-full"].read_text(encoding="utf-8") == _fresh_text(
        "node-exporter-full"
    )
