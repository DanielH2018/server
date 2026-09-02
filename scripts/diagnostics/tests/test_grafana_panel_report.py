#!/usr/bin/env python3
"""Paired red-proofs for the Grafana panel classifier.

Every rule gets one payload it must ACCEPT and one it must REJECT — a classifier that fired
on everything and one that fired on nothing are indistinguishable from the passing side
alone, which is how `volume-claim` shipped behind 16 green tests and then matched 0 of 25
claims. These run in CI: they carry no `ui` marker and start no browser.

The payloads are the real ones, sampled on 2026-08-30 through `ui_mcp.sh` against the live
cluster, not invented.
"""

from grafana_panel_report import FLAGGED, NOT_MOUNTED, OK, classify

# `longhorn-storage`, mounted and healthy.
RENDERED = {
    "ready": "complete",
    "path": "/d/longhorn-storage/longhorn-e28094-storage",
    "testids": 131,
    "headers": 13,
    "rows": 4,
    "statusError": 0,
    "pluginNotFound": False,
}

# The same dashboard in the same run, on a navigate where the React app never booted.
UNMOUNTED = {
    "ready": "complete",
    "path": "/d/longhorn-storage/",
    "testids": 0,
    "headers": 0,
    "rows": 0,
    "statusError": 0,
    "pluginNotFound": False,
}

# `crowdsec-details-per-machine`: 4 panels, every one of them a `row`.
ROW_ONLY = {
    "ready": "complete",
    "path": "/d/6L2GdB47z/crowdsec-details-per-machine",
    "testids": 121,
    "headers": 0,
    "rows": 12,
    "statusError": 0,
    "pluginNotFound": False,
}


def test_a_rendered_dashboard_is_clean():
    v = classify(RENDERED, min_headers=10)
    assert v.status == OK, v.detail
    assert not v.retryable


def test_a_dashboard_below_its_header_floor_is_flagged():
    """The 2026-08-22 shape: mounted, provisioned, drawing nothing."""
    v = classify(RENDERED | {"headers": 2}, min_headers=10)
    assert v.status == FLAGGED
    assert "expected at least 10" in v.detail


def test_an_unmounted_page_is_retryable_not_a_verdict():
    v = classify(UNMOUNTED, min_headers=10)
    assert v.status == NOT_MOUNTED
    assert v.retryable, (
        "an un-mounted page must be retried, never reported as a failure"
    )


def test_a_mounted_page_is_not_retryable():
    """The rejecting half of the retry rule: a real failure must not be retried away."""
    v = classify(RENDERED | {"headers": 0}, min_headers=10)
    assert not v.retryable
    assert v.status == FLAGGED


def test_grafanas_chrome_without_a_dashboard_is_retryable():
    """The signature `--disable-http2` was added to prevent: Grafana's own navigation and
    menus render (27-53 testids), the dashboard request 421s, and the URL never gains its
    slug. A testid count alone reads this as a mounted page with no panels."""
    v = classify(
        UNMOUNTED | {"testids": 53, "path": "/d/longhorn-storage/"}, min_headers=10
    )
    assert v.status == NOT_MOUNTED
    assert v.retryable


def test_a_dashboard_that_drew_nothing_is_worth_a_second_load():
    v = classify(RENDERED | {"headers": 0, "rows": 0}, min_headers=10)
    assert v.status == FLAGGED, "still a finding if it persists"
    assert v.worth_renavigating, (
        "an empty render deserves a re-navigate before reporting"
    )


def test_a_partial_render_is_reported_without_retrying():
    """The rejecting half. Some panels drawn and some missing is a finding, and loading
    again would average over exactly the breakage the tier exists to see."""
    v = classify(RENDERED | {"headers": 3, "rows": 4}, min_headers=10)
    assert v.status == FLAGGED
    assert not v.worth_renavigating


def test_a_row_only_dashboard_that_drew_nothing_is_worth_a_second_load():
    v = classify(ROW_ONLY | {"rows": 0}, min_headers=0)
    assert v.status == FLAGGED
    assert v.worth_renavigating


def test_a_row_only_dashboard_is_clean():
    v = classify(ROW_ONLY, min_headers=0)
    assert v.status == OK, v.detail


def test_a_row_only_dashboard_with_no_rows_is_flagged():
    v = classify(ROW_ONLY | {"rows": 0}, min_headers=0)
    assert v.status == FLAGGED
    assert "no rows" in v.detail


def test_a_missing_panel_plugin_is_flagged():
    v = classify(RENDERED | {"pluginNotFound": True}, min_headers=10)
    assert v.status == FLAGGED
    assert "Panel plugin not found" in v.detail


def test_a_panel_carrying_a_real_error_is_flagged():
    v = classify(
        RENDERED
        | {"statusError": 3, "errorTexts": ["Datasource abc123 was not found"]},
        min_headers=10,
    )
    assert v.status == FLAGGED
    assert "Datasource abc123 was not found" in v.detail


def test_panels_reading_no_data_are_clean():
    """The rejecting half. Grafana marks an EMPTY panel with the same testid it marks a
    broken one, so counting the testid flags every dashboard whose window is quiet."""
    v = classify(
        RENDERED | {"statusError": 10, "errorTexts": ["No data", "No data"]},
        min_headers=10,
    )
    assert v.status == OK, v.detail


def test_a_real_error_alongside_no_data_is_still_flagged():
    v = classify(
        RENDERED | {"statusError": 4, "errorTexts": ["No data", "Query timeout"]},
        min_headers=10,
    )
    assert v.status == FLAGGED
    assert "Query timeout" in v.detail
    assert "No data" not in v.detail


def test_one_error_repeated_across_panels_is_reported_once():
    v = classify(
        RENDERED | {"statusError": 6, "errorTexts": ["Query timeout"] * 6},
        min_headers=10,
    )
    assert v.detail.count("Query timeout") == 1


def test_an_unmounted_page_outranks_every_other_rule():
    """Zero testids means nothing on the page can be trusted, including the error counts."""
    v = classify(UNMOUNTED | {"pluginNotFound": True, "statusError": 9}, min_headers=10)
    assert v.status == NOT_MOUNTED
