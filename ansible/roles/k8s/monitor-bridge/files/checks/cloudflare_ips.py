"""Cloudflare published IP ranges against the `cloudflare_ips` allowlist, for monitor-bridge.

That allowlist is baked into traefik's static config as the forwardedHeaders.trustedIPs of both
entrypoints, and since #1974 into netpol-baseline's ipBlock allow on traefik's :8443. A range
Cloudflare adds that the list lacks is DENIED at the policy and untrusted at the proxy, with no
error anywhere: CrowdSec bans land on an edge IP, Authelia logs the edge as the client, Home
Assistant's X-Forwarded-For trust chain reads the edge. This check alerts on the drift; it never
applies a fetched list.

It ran as a daily root cron on daniel-box from 2026-08-15 to 2026-09-19 (k8s/traefik's
cloudflare-ip-drift.sh), pushing its tile directly. Moved here because it needs no host state
at all — two anonymous fetches against a list the env carries — and the bridge already has the
shape a slow-moving, cheap probe wants: a SUCCESS is cached for CLOUDFLARE_IPS_PROBE_INTERVAL_S
(a day; Cloudflare changes these ranges on a multi-year cadence), and a FAILURE is never cached,
so a red tile re-probes every 300 s cycle and clears within one cycle of the list being fixed
rather than at the next day's slot. Same idiom as `checks/r2.py`; the rationale for the pair of
idioms is in bridge/config_io.py's header.

Reads config as `cfg.X`. The fetch and the cache are parameters of `cloudflare_ips_drift`, so
a test hands in a canned fetch and a fresh dict rather than patching this module. `_probe` lives
beside `cloudflare_ips_drift`, the only code that mutates it.
"""

from collections.abc import Callable
import time
import urllib.request

from bridge.common import HTTP_TIMEOUT
from bridge.config import Config
from bridge.parsing import describe_fetch_failure

IPS_URLS = ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6")

# Below this many published ranges the fetch is treated as bad rather than as a shrunken list:
# Cloudflare publishes 15 IPv4 and 7 IPv6 ranges as of 2026-09, and a page that answers 200
# with a stub body must not read as "Cloudflare dropped its ranges".
MIN_PLAUSIBLE_RANGES = 10


def fetch_ranges(urls: tuple[str, ...] = IPS_URLS) -> list[str]:
    """Every non-blank line of the published range pages, in page order.

    Raises RuntimeError naming the endpoint on any transport or HTTP failure, so the caller's
    verdict says which page did not answer rather than reporting an empty list as drift.
    """
    ranges: list[str] = []
    for url in urls:
        req = urllib.request.Request(url, headers={"User-Agent": "monitor-bridge"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
        except Exception as e:
            raise RuntimeError(describe_fetch_failure(url, e)) from e
        ranges.extend(line.strip() for line in body.splitlines() if line.strip())
    return ranges


def cloudflare_ips_verdict(
    live: list[str], expected: frozenset[str]
) -> tuple[bool, str]:
    """(ok, msg) for a fetched range list against the declared allowlist. Pure."""
    live_set = frozenset(live)
    if len(live_set) < MIN_PLAUSIBLE_RANGES:
        return False, (
            "Cloudflare published only %d ranges — implausible, treating as a bad fetch"
            % len(live_set)
        )
    if live_set == expected:
        return True, "cloudflare_ips matches upstream (%d CIDRs)" % len(live_set)
    added = sorted(live_set - expected)
    stale = sorted(expected - live_set)
    return False, (
        "cloudflare_ips DRIFTED from upstream — added:[%s] stale:[%s] — update "
        "cloudflare_ips in group_vars/all.yml and redeploy traefik and netpol-baseline"
        % (" ".join(added), " ".join(stale))
    )


# ts=None means never probed; see checks/r2.py for why not 0.0.
_probe = {"ts": None, "ok": True, "msg": ""}


def cloudflare_ips_drift(
    cfg: Config,
    now: float | None = None,
    fetch: Callable[[], list[str]] = fetch_ranges,
    probe: dict | None = None,
) -> tuple[bool, str]:
    """Throttled allowlist drift check. (ok, msg).

    A success is cached for CLOUDFLARE_IPS_PROBE_INTERVAL_S. A failure — drift, a bad page or a
    fetch that did not answer — is not, so the next cycle re-probes and the tile clears as soon
    as the list is fixed. The one-cycle blip a transient fetch failure would page on is
    absorbed by STARTUP_GRACE and the tile's own beat window.

    `fetch` and `probe` are the seams: a test passes a canned fetch and a fresh cache dict
    rather than patching this module, which the repo's monkeypatch ratchet does not allow a
    new test module to do.
    """
    probe = _probe if probe is None else probe
    if not cfg.CLOUDFLARE_IPS_EXPECTED:
        return True, "cloudflare_ips drift check disabled (no expected list)"
    now = now if now is not None else time.time()
    if (
        probe["ts"] is not None
        and probe["ok"]
        and now - probe["ts"] < cfg.CLOUDFLARE_IPS_PROBE_INTERVAL_S
    ):
        return True, "%s (checked %.0fh ago)" % (
            probe["msg"],
            (now - probe["ts"]) / 3600,
        )
    try:
        live = fetch()
    except RuntimeError as e:
        ok, msg = (
            False,
            "failed to fetch Cloudflare ranges — allowlist unverified: %s" % e,
        )
    else:
        ok, msg = cloudflare_ips_verdict(live, cfg.CLOUDFLARE_IPS_EXPECTED)
    probe["ts"] = now
    probe["ok"] = ok
    probe["msg"] = msg
    return ok, msg


def check_cloudflare_ips_drift(cfg: Config) -> tuple[bool, str]:
    return cloudflare_ips_drift(cfg)
