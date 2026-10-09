"""The WAN-reachability gate: is anything outside this house reachable at all?

An internet outage had no gate before this, so every check that reaches the internet paged on
its own. On 2026-09-18 from 05:05 one WAN outage turned 11 tiles red inside 90 minutes —
b2_reachable, r2_usage, discord, kuma_notify_failures, crowdsec-home-allowlist,
cloudflare-ip-drift, github-ruleset-drift, release-staleness-check, docs-refresh,
swallowed_verdicts and gitops_alive (#2784). This gate turns that into one page.

BY HOSTNAME, against two independent providers — never an anycast IP. That 2026-09-18 outage
included DNS failure ("failed to resolve public IPv4 from ipify"), and an IP-only probe stays
green through a DNS-only outage while every dependent fails: the exact storm the gate exists to
suppress. The hostname makes resolution part of what is probed.

TWO providers, and the gate is down only when BOTH fail. One provider's outage is that
provider's problem and must not silence a dependent reading a different one — Cloudflare's own
tiles would go green on a Cloudflare outage, which is backwards. Requiring both to fail means
the gate reports the one fault it can actually attribute to this house's link.

The two default endpoints are `bridge/config_io.py:WAN_PROBE_DEFAULT` — Cloudflare's trace
page and Google's connectivity-check 204, both endpoints their owners keep cheap. The default
lives there because `bridge/` is a leaf this module imports, not the other way round.

Measured from daniel-server 2026-09-27, three runs each: cloudflare.com/cdn-cgi/trace answers
in 0.095-0.100 s total (0.002-0.028 s of that DNS), www.google.com/generate_204 in
0.074-0.122 s. Both are far inside HTTP_TIMEOUT, and the second is only reached when the first
fails, so a healthy cycle costs one request of about 0.1 s.
"""

from collections.abc import Callable
import urllib.request

from bridge.common import HTTP_TIMEOUT
from bridge.config import Config
from bridge.sources import Sources
from bridge.parsing import describe_fetch_failure


def _fetch_ok(url: str) -> None:
    """Fetch `url`, raising RuntimeError naming the endpoint on any transport or HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-bridge"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read(1)
    except Exception as e:
        raise RuntimeError(describe_fetch_failure(url, e)) from e


def wan_verdict(
    urls: tuple[str, ...],
    fetch: Callable[[str], None] = _fetch_ok,
) -> tuple[bool, str]:
    """Pure-ish: probe each URL in turn, stopping at the first that answers.

    Args:
      urls: The endpoints to try, in order. Empty disables the gate.
      fetch: Called with one URL; returns None when it answered and raises otherwise.

    Returns:
      (ok, msg). `ok` is True as soon as one endpoint answers, so a healthy cycle makes one
      request. False only when every endpoint failed, and the message names each failure —
      an operator reading the tile needs to see whether it was DNS or connect.
    """
    if not urls:
        return True, "WAN reachability check disabled (no WAN_PROBE_URLS)"
    failures = []
    for url in urls:
        try:
            fetch(url)
        except Exception as e:
            failures.append(str(e))
            continue
        return True, "WAN reachable via %s" % url
    return False, "no WAN: %s" % "; ".join(failures)


def check_wan_reachable(cfg: Config, src: Sources) -> tuple[bool, str]:
    """The gate body: can this host reach the public internet at all?"""
    return wan_verdict(cfg.WAN_PROBE_URLS)
