#!/usr/bin/env python3
"""Tests for export_grafana_dashboards pure helpers (slug + datasource normalize).

These are the live-DB→code drift helpers: `slug` names the on-disk file, and `normalize`
rewrites stale datasource uids onto the canonical ones + strips the ephemeral query `key`.
A regression here churns every exported dashboard or leaks a stale uid (silent "No data").
The network/docker-exec path (gapi/main) is intentionally not exercised here.

Run: uv run pytest scripts/test_export_grafana_dashboards.py
"""
import export_grafana_dashboards as eg


# --- slug -------------------------------------------------------------------

def test_slug_lowercases_and_hyphenates():
    assert eg.slug("My Dashboard") == "my-dashboard"


def test_slug_strips_punctuation_and_edges():
    assert eg.slug("  CrowdSec / Overview!! ") == "crowdsec-overview"


def test_slug_collapses_separator_runs():
    assert eg.slug("a---b__c") == "a-b-c"


# --- normalize: datasource uid remap ----------------------------------------

def test_normalize_remaps_stale_string_datasource():
    obj = {"datasource": "IH0jqv6nz"}  # the known stale Prometheus uid
    eg.normalize(obj)
    assert obj["datasource"] == eg.PROM_UID


def test_normalize_remaps_stale_dict_datasource_uid():
    obj = {"datasource": {"type": "prometheus", "uid": "IH0jqv6nz"}}
    eg.normalize(obj)
    assert obj["datasource"]["uid"] == eg.PROM_UID


def test_normalize_leaves_canonical_datasource_untouched():
    obj = {"datasource": {"uid": eg.PROM_UID}}
    eg.normalize(obj)
    assert obj["datasource"]["uid"] == eg.PROM_UID


# --- normalize: ephemeral query `key` ---------------------------------------

def test_normalize_drops_key_on_query_target():
    obj = {"refId": "A", "key": "random-uuid", "expr": "up"}
    eg.normalize(obj)
    assert "key" not in obj and obj["refId"] == "A"


def test_normalize_keeps_key_on_non_target_dict():
    obj = {"key": "keep-me"}  # no refId -> not a query target
    eg.normalize(obj)
    assert obj["key"] == "keep-me"


def test_normalize_recurses_into_nested_panels_and_lists():
    obj = {"panels": [{"targets": [{"refId": "A", "key": "x", "datasource": "IH0jqv6nz"}]}]}
    eg.normalize(obj)
    target = obj["panels"][0]["targets"][0]
    assert "key" not in target and target["datasource"] == eg.PROM_UID
