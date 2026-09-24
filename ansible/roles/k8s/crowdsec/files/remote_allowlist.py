#!/usr/bin/env python3
"""Keep the LAPI `remote-ips` allowlist tracking the operator's authenticated remote exits.

The home-allowlist cron covers the home address, which daniel-box can look up for itself.
A remote session — a VPN exit, a phone — arrives from an address nothing on the server side
can predict, and the only evidence of who is behind it is that the session is authenticated.
Until 2026-09-21 that case was a hand-pinned /32 in `crowdsec-trusted-remote-whitelist.yaml`,
and the operator self-banned every time the exit rotated (#2123).

The signal is Traefik's access log, not Authelia's. A login is logged once per session and a
session outlives it by days, so 48 hours of Authelia logs held zero logins while the operator
browsed throughout. Every request through a router that carries the `authelia` forwardAuth
middleware is checked instead: Authelia answers an unauthenticated one with its own 302, so a
2xx (or 304) on such a router IS an authenticated request, refreshed on every page load.

Router names are derived, not listed. Traefik names a kubernetescrd router
`<namespace>-<ingressroute>-<sha256(match)[:10 bytes]>@kubernetescrd`, so the set is rebuilt
from the live IngressRoutes each run and a new service that attaches the middleware is
covered without an edit here. The hash is of the ROUTE, which is what keeps
`karakeep-public-api-trpc` (rate-limit only, the app's own auth) out while `karakeep-public`
(authelia) is in — matching on Host would allowlist anyone who can reach the public API.

Bounds, because an allowlist entry also suppresses CAPI decisions for that address and a
shared VPN exit is exactly the kind of address CAPI flags: an address is exempt for TTL after
its last authenticated sighting, refreshed only when under half of that remains; the list is
capped, and reaching the cap is a DOWN that names the entries rather than a silent skip; and
private, loopback and CGNAT addresses never enter, so an Authelia LAN bypass rule cannot feed
it. The way out is `cscli allowlists remove remote-ips <ip>`, or letting the entry lapse; the
way to see it is `cscli allowlists inspect remote-ips`.

Runs from the cron wrapper `crowdsec-update-remote-allowlist.sh` as root on daniel-box; the
wrapper owns the Kuma push and the syslog line, this module owns the decision. Its last
stdout line is the summary the wrapper pushes. The functions above `main` are pure and are
what `tests/test_remote_allowlist.py` exercises.
"""

import hashlib
import ipaddress
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

ALLOWLIST = "remote-ips"
AUTH_MIDDLEWARE = "authelia"
NAMESPACE = "homelab"
# Sighting window. The cron runs every 5 min; 6 min overlaps the previous run so a request on
# the boundary is seen once by each rather than by neither.
LOG_WINDOW = "6m"
# An exit is exempt for a week after its last authenticated request. Sized from the incidents:
# the same exit banned the operator on 2026-09-19 and again 2026-09-21, two days apart, and the
# 2026-08-09 entry it replaced held for six weeks. The refresh threshold is half the TTL so a
# daily-used exit costs one rewrite every 3.5 days, not one per run.
TTL = timedelta(hours=168)
REFRESH_BELOW = TTL / 2
# The cap is the bound on the trade-off named in the module docstring. Eight is several times
# the number of exits one operator uses in a week; hitting it means something is feeding the
# list that is not the operator, and the DOWN message lists the entries so that can be read.
CAP = 8
K3S = "/usr/local/bin/k3s"
OK_STATUSES = frozenset(range(200, 300)) | {304}


def router_name(namespace, name, match):
    """Traefik's name for one route of a kubernetescrd IngressRoute.

    `makeServiceKey` in Traefik's CRD provider: `<ingress name>-%.10x` of sha256 over the match
    rule, prefixed with the namespace. Verified against the live router
    `homelab-karakeep-public-api-trpc-121339d25e3ce6f2e046@kubernetescrd` on 2026-09-21.
    """
    digest = hashlib.sha256(match.encode()).hexdigest()[:20]
    return f"{namespace}-{name}-{digest}@kubernetescrd"


def authenticated_routers(ingressroutes, middleware=AUTH_MIDDLEWARE):
    """The router names whose route lists `middleware` — a `kubectl get ingressroute -A -o json` body."""
    names = set()
    for item in ingressroutes.get("items", []):
        meta = item.get("metadata", {})
        for route in item.get("spec", {}).get("routes", []):
            attached = {m.get("name") for m in route.get("middlewares") or []}
            if middleware in attached and route.get("match"):
                names.add(router_name(meta["namespace"], meta["name"], route["match"]))
    return frozenset(names)


def is_remote_client(host):
    """A public unicast address — the only kind CrowdSec would ban and the LAN allowlist lacks."""
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def authenticated_clients(lines, routers):
    """Client addresses with an authenticated request among `lines` (Traefik JSON access log)."""
    seen = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError, TypeError:
            continue
        if not isinstance(entry, dict) or entry.get("RouterName") not in routers:
            continue
        if entry.get("DownstreamStatus") not in OK_STATUSES:
            continue
        # ClientHost is Traefik's whole X-Forwarded-For header, recorded before any entrypoint
        # middleware runs. From a Cloudflare source it reads `<client-sent>, <client>`:
        # Cloudflare appends the address it saw, so only the RIGHTMOST entry is not client-chosen.
        # From any other source forwardedHeaders has already dropped the header, leaving the
        # connection address. The crowdsecurity/traefik-logs parser takes the same entry, so
        # this list holds the address CrowdSec bans (#2446).
        host = (entry.get("ClientHost") or "").rsplit(",", 1)[-1].strip()
        if is_remote_client(host):
            seen.add(host)
    return frozenset(seen)


def parse_expiration(raw):
    """An allowlist item's expiry as an aware datetime, or None when it never expires."""
    if not raw or str(raw).startswith("0001-01-01"):
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Plan:
    add: list = field(default_factory=list)  # new addresses
    refresh: list = field(
        default_factory=list
    )  # present, under REFRESH_BELOW left: remove+add
    prune: list = field(default_factory=list)  # present, already expired
    over_cap: list = field(default_factory=list)  # seen, would exceed CAP, NOT added
    live: list = field(default_factory=list)  # present and unexpired after this run

    def summary(self):
        parts = [f"{len(self.live)} live"]
        if self.add:
            parts.append("added " + ",".join(self.add))
        if self.refresh:
            parts.append("refreshed " + ",".join(self.refresh))
        if self.prune:
            parts.append("pruned " + ",".join(self.prune))
        return f"remote-ips: {'; '.join(parts)}"


def plan(seen, items, now, ttl=TTL, refresh_below=REFRESH_BELOW, cap=CAP):
    """Decide the writes for this run from the sightings and the LAPI's current items.

    `items` is `cscli allowlists inspect -o json`'s `items` list. An item whose expiry cannot be
    read counts as due for refresh: one extra rewrite is cheaper than an entry that quietly
    lapsed. Additions stop at `cap`, and the addresses left out are reported, not dropped.
    """
    live = []
    result = Plan()
    present = {}
    for item in items:
        value = item.get("value")
        if not value:
            continue
        expires = parse_expiration(item.get("expiration"))
        if expires is not None and expires <= now:
            result.prune.append(value)
            continue
        present[value] = expires
        live.append(value)
    for value in sorted(seen):
        if value in present:
            expires = present[value]
            if expires is None or expires - now < refresh_below:
                result.refresh.append(value)
        elif len(live) < cap:
            result.add.append(value)
            live.append(value)
        else:
            result.over_cap.append(value)
    result.live = sorted(live)
    return result


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def cscli(runner, *args):
    return runner(
        [
            K3S,
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            "deploy/crowdsec",
            "-c",
            "crowdsec",
            "--",
            "cscli",
            *args,
        ]
    )


def main(runner=run, now=None):
    now = now or datetime.now(UTC)
    ingressroutes = json.loads(
        runner([K3S, "kubectl", "get", "ingressroute", "-A", "-o", "json"])
    )
    routers = authenticated_routers(ingressroutes)
    if not routers:
        print(
            "no router carries the authelia middleware — refusing to treat every 2xx as authenticated"
        )
        return 1
    log = runner(
        [
            K3S,
            "kubectl",
            "-n",
            NAMESPACE,
            "logs",
            "deploy/traefik",
            "-c",
            "access-log-rotate",
            f"--since={LOG_WINDOW}",
        ]
    )
    seen = authenticated_clients(log.splitlines(), routers)
    inspected = json.loads(
        cscli(runner, "allowlists", "inspect", ALLOWLIST, "-o", "json") or "{}"
    )
    decided = plan(seen, inspected.get("items") or [], now)
    ttl_arg = f"{int(TTL.total_seconds() // 3600)}h"
    for value in decided.prune + decided.refresh:
        cscli(runner, "allowlists", "remove", ALLOWLIST, value)
    for value in decided.refresh + decided.add:
        cscli(
            runner,
            "allowlists",
            "add",
            ALLOWLIST,
            value,
            "-e",
            ttl_arg,
            "-d",
            f"authenticated session seen {now:%Y-%m-%d}",
        )
    if decided.over_cap:
        print(
            f"remote-ips at cap ({CAP}): live {','.join(decided.live)}; not added {','.join(decided.over_cap)}"
        )
        return 1
    print(decided.summary())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()
        # Name the verb that failed (`cscli allowlists add`, `kubectl logs`), never the
        # address it was given — the summary line lands in Kuma and syslog.
        words = list(exc.cmd)
        start = words.index("cscli") if "cscli" in words else 1
        verb = " ".join(w for w in words[start : start + 3] if not w.startswith("-"))
        print(f"{verb} failed rc={exc.returncode}: {tail[-1] if tail else 'no output'}")
        sys.exit(1)
