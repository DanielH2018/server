#!/usr/bin/env python3
"""Tests for probe.py — the consolidated read-only homelab diagnostics wrapper.

probe.py replaces a pile of one-off `curl http://<container-ip>:<port>/...` and
`openssl s_client` commands that each became a dead, never-reused allow-list
entry. The whole point is one allow-listed surface, so the routing + URL building
must be correct. Network/Docker calls are injected out via a fake resolver, so
these tests are hermetic.

Run: uv run pytest scripts/test_probe.py
"""
import importlib.util
import os

import pytest

_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
_spec = importlib.util.spec_from_file_location("probe", _MOD)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# Fake resolver: maps container name -> a recognizable IP. A wrong container name
# raises KeyError, so a misrouted subcommand fails loudly.
IPS = {"prometheus": "10.0.0.1", "loki": "10.0.0.2", "scrutiny": "10.0.0.3"}
fake_resolve = IPS.__getitem__


# --- URL builders -----------------------------------------------------------

def test_prom_query_url_encodes_promql():
    url = probe.prom_query_url("10.0.0.1", "up == 0")
    assert url == "http://10.0.0.1:9090/api/v1/query?query=up+%3D%3D+0"


def test_prom_targets_url():
    assert probe.prom_targets_url("10.0.0.1") == "http://10.0.0.1:9090/api/v1/targets"


def test_loki_labels_url():
    assert probe.loki_labels_url("10.0.0.2") == "http://10.0.0.2:3100/loki/api/v1/labels"


def test_loki_query_url_encodes_logql_and_limit():
    url = probe.loki_query_url("10.0.0.2", '{job="x"}', 50)
    assert url == "http://10.0.0.2:3100/loki/api/v1/query_range?query=%7Bjob%3D%22x%22%7D&limit=50"


def test_scrutiny_url():
    assert probe.scrutiny_url("10.0.0.3") == "http://10.0.0.3:8080/api/summary"


def test_pi_url():
    assert probe.pi_url("fs") == "http://daniel-pi.lan:61208/api/4/fs"


# --- low-level argv / parsing helpers ---------------------------------------

def test_curl_argv():
    assert probe.curl_argv("http://x") == ["curl", "-sS", "--max-time", "10", "http://x"]


def test_inspect_ip_argv_targets_the_container():
    argv = probe.inspect_ip_argv("loki")
    assert argv[:3] == ["docker", "inspect", "-f"]
    assert argv[-1] == "loki"
    assert ".IPAddress" in argv[3]


def test_parse_ip_takes_first_nonempty_token():
    assert probe.parse_ip("172.19.0.12 172.18.0.5 \n") == "172.19.0.12"


def test_parse_ip_returns_none_when_no_ip():
    assert probe.parse_ip("   \n") is None


# --- plan(): routing for each subcommand ------------------------------------

def test_plan_metric_resolves_prometheus():
    stages = probe.plan(["metric", "up == 0"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.1:9090/api/v1/query?query=up+%3D%3D+0")]


def test_plan_targets_resolves_prometheus():
    stages = probe.plan(["targets"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.1:9090/api/v1/targets")]


def test_plan_loki_labels_resolves_loki():
    stages = probe.plan(["loki-labels"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.2:3100/loki/api/v1/labels")]


def test_plan_loki_query_with_limit():
    stages = probe.plan(["loki-query", '{job="x"}', "--limit", "50"], fake_resolve)
    assert stages == [probe.curl_argv(probe.loki_query_url("10.0.0.2", '{job="x"}', 50))]


def test_plan_scrutiny_resolves_scrutiny():
    stages = probe.plan(["scrutiny"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.3:8080/api/summary")]


def test_plan_pi_does_not_resolve_docker():
    # Pi glances is reached by hostname, so the resolver must NOT be consulted.
    def boom(_):
        raise AssertionError("pi must not resolve a container IP")
    stages = probe.plan(["pi", "fs"], boom)
    assert stages == [probe.curl_argv("http://daniel-pi.lan:61208/api/4/fs")]


def test_plan_cert_defaults_port_and_sni_to_host():
    stages = probe.plan(["cert", "homepage.daniel-hunter.com"], fake_resolve)
    assert stages == probe.cert_stages("homepage.daniel-hunter.com", 443, "homepage.daniel-hunter.com")


def test_plan_cert_explicit_port_and_sni():
    stages = probe.plan(["cert", "10.0.0.161:443", "--sni", "homepage.daniel-hunter.com"], fake_resolve)
    assert stages == probe.cert_stages("10.0.0.161", 443, "homepage.daniel-hunter.com")


def test_cert_stages_is_a_two_stage_pipeline():
    s1, s2 = probe.cert_stages("h", 443, "h")
    assert s1[:2] == ["openssl", "s_client"]
    assert "h:443" in s1
    assert s2[:2] == ["openssl", "x509"]
