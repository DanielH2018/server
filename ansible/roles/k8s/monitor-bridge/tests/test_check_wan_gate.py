"""The WAN-reachability gate: the verdict and the membership.

Without a gate, one internet outage pages every dependent tile. The behaviour that has to hold is
narrow: down only when EVERY endpoint fails, and the dependents named in the table. What a down
gate does to them is `test_check.py`.
"""

import checks.wan
from bridge.config_io import WAN_PROBE_DEFAULT
from check_table import CHECKS


def _fetch(failing: set[str]):
    """A fetch stub that raises for the named URLs and returns for the rest."""

    def fetch(url):
        if url in failing:
            raise RuntimeError("%s: connect timed out" % url)

    return fetch


URLS = ("https://a.example/probe", "https://b.example/probe")


def test_one_endpoint_answering_is_reachable():
    ok, msg = checks.wan.wan_verdict(URLS, _fetch({URLS[0]}))
    assert ok
    assert URLS[1] in msg


def test_the_first_answer_stops_the_probe():
    """A healthy cycle costs one request, not one per provider."""
    tried = []

    def fetch(url):
        tried.append(url)

    ok, _ = checks.wan.wan_verdict(URLS, fetch)
    assert ok
    assert tried == [URLS[0]]


def test_every_endpoint_failing_is_down_and_names_each():
    ok, msg = checks.wan.wan_verdict(URLS, _fetch(set(URLS)))
    assert not ok
    assert URLS[0] in msg and URLS[1] in msg


def test_no_urls_disables_the_gate():
    ok, msg = checks.wan.wan_verdict(())
    assert ok
    assert "disabled" in msg


def test_the_default_endpoints_are_two_independent_providers():
    """A single provider's outage must not read as "this house has no link".

    The gate is down only when every endpoint fails, so two URLs under one domain would make a
    provider outage suppress the dependents reading the OTHER provider — Cloudflare's own tiles
    going green during a Cloudflare outage, which is backwards.
    """
    hosts = {u.split("/")[2] for u in WAN_PROBE_DEFAULT.split(",")}
    assert len(hosts) == 2
    suffixes = {".".join(h.split(".")[-2:]) for h in hosts}
    assert len(suffixes) == 2, "both default endpoints sit under %s" % suffixes


def test_the_default_endpoints_are_hostnames_not_addresses():
    """DNS failure is part of the outage this gate reports.

    An outage can include "failed to resolve public IPv4 from ipify". A probe against
    an anycast literal stays green through a DNS-only outage while every dependent fails.
    """
    for url in WAN_PROBE_DEFAULT.split(","):
        host = url.split("/")[2]
        assert not host.replace(".", "").isdigit(), host


def test_wan_dependents_are_derived_from_the_table_and_not_empty():
    dependents = {row.name for row in CHECKS if row.gate == "wan_reachable"}
    assert {"r2_usage", "cloudflare_ips_drift", "discord"} <= dependents
