"""The WAN-reachability gate: the verdict, the membership, and what it suppresses.

Without a gate, one internet outage pages every dependent tile. The behaviour that has to hold is narrow: down only when EVERY endpoint
fails, one page rather than none, and the dependents held green with a `skipped` message.
"""

import checks.wan
import gates
import registry
from _check_gate_helpers import wire_run_once_reachability
from bridge.config_io import WAN_PROBE_DEFAULT


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


def test_wan_dependent_names_real_checks_and_is_not_empty():
    names = {c.name for c in registry.build_checks()}
    assert gates.WAN_DEPENDENT <= names
    assert {"r2_usage", "cloudflare_ips_drift", "discord"} <= gates.WAN_DEPENDENT


def test_run_once_suppresses_wan_dependents_and_pages_once(cfg):
    """One page for the outage, not eleven — and not zero."""
    ran, pushes = wire_run_once_reachability(
        cfg,
        ["r2_usage", "discord", "targets"],
        wan_result=(False, "no WAN: both endpoints failed"),
        wan_dependent={"r2_usage", "discord"},
    )
    assert not ({"r2_usage", "discord"} & set(ran))
    assert "targets" in ran
    by_tok = {t: (ok, m) for t, ok, m in pushes}
    for name in ("r2_usage", "discord"):
        ok, msg = by_tok["tok_%s" % name]
        assert ok is True
        assert "WAN unreachable" in msg
    # Not zero pages: the WAN tile itself still goes down with its own message.
    assert any(ok is False and "no WAN" in msg for _, ok, msg in pushes)


def test_run_once_runs_wan_dependents_when_the_link_is_up(cfg):
    """The reject half: a healthy gate must suppress nothing."""
    ran, _ = wire_run_once_reachability(
        cfg,
        ["r2_usage", "discord"],
        wan_dependent={"r2_usage", "discord"},
    )
    assert set(ran) == {"r2_usage", "discord"}


def test_a_raising_probe_is_a_down_gate(cfg):
    """The real outage path: the probe raises rather than returning a pair."""
    ran, _ = wire_run_once_reachability(
        cfg,
        ["r2_usage"],
        wan_result=RuntimeError("Temporary failure in name resolution"),
        wan_dependent={"r2_usage"},
    )
    assert "r2_usage" not in ran
