#!/usr/bin/env python3
"""Keep the LAPI `home-ips` allowlist holding the home public IPv4 address and IPv6 /64.

An allowlisted address raises no decision, local or CAPI, so browsing the public path from
home never trips the WAF. Both families are allowlisted. IPv4 is the single NAT'd home
address. IPv6 is the home LAN's /64: no NAT, every device routable, and privacy extensions
rotate addresses inside the prefix, so a /128 would cover one interface of one machine for a
few hours. A burst test egressing over IPv6 banned the homelab's own address on 2026-08-06.

The addresses come from the `crowdsec-home-ips` ConfigMap, which the `crowdsec-home-ips`
CronJob writes from daniel-box's host network (`home_ips_discover.py`). This job runs in the
`crowdsec-jobs` sidecar of the LAPI pod, started by `crowdsec_jobs.py`, which owns the Kuma
push and the log line. The split is the operator's ruling of 2026-10-18 on #5701: the sidecar
shares the pod's IPv4-only network and cannot see the /64 itself.

**The allowlist is its own state.** Each run reads the stored entries with `cscli allowlists
inspect home-ips` and compares them with the ConfigMap. This job owns every entry in
`home-ips`, so for each family it adds the wanted value and removes any other entry of that
family. **An empty wanted value removes nothing**, so a failed or empty IPv6 lookup never
removes a stored /64, and the IPv4 entry syncs whatever happened to IPv6 (#1248).

The verdict, first match wins:

- down when `looked_up_at` is older than `HOME_IPS_STALE_AFTER_S`, three CronJob periods, so
  a dead discovery pages;
- down when a cscli call failed, or when either family's lookup failed for any reason but an
  unreachable WAN. A stale /64 can rotate out from under a green tile, so an IPv6 fault is
  reported even when the IPv4 half synced cleanly;
- up, naming the skip, when the IPv4 lookup failed because no WAN probe answered. The WAN
  Reachable tile owns that fault (#2793);
- up otherwise, naming any change.

Usage:

    python3 /jobs/home_allowlist.py

It reads the ConfigMap through the sidecar's `kubectl`, as the pod's `crowdsec-jobs`
ServiceAccount, and edits the allowlist through the sidecar's own `cscli`. Inputs from the
environment: `HOME_IPS_CONFIGMAP` names the ConfigMap and `HOME_IPS_STALE_AFTER_S` bounds its
age. Exit 0 means the verdict is up and exit 1 that it is down; the last stdout line says why.
`-h` and `--help` print this text and exit 0.
"""

import ipaddress
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ALLOWLIST = "home-ips"
NAMESPACE = "homelab"
CSCLI = "/tools/cscli"
KUBECTL = "/tools/kubectl"
# Must match home_ips_discover.WAN_UNREACHABLE; tests/test_home_allowlist.py holds them equal.
WAN_UNREACHABLE = "WAN unreachable"


def network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """`value` as a network, an address as its /32 or /128, or None when it is neither."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def entry(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> str:
    """The text an allowlist entry for `net` takes: a bare address for one host, else the range."""
    host = net.prefixlen == net.max_prefixlen
    return str(net.network_address) if host else str(net)


@dataclass
class Plan:
    """The writes for one run: stored entries to remove, values to add, and a line per change."""

    remove: list = field(default_factory=list)
    add: list = field(default_factory=list)
    changes: list = field(default_factory=list)


def plan(wanted: Mapping[int, str], stored: list[str]) -> Plan:
    """Decide the writes from the wanted value per IP version and the stored entries.

    `wanted` maps 4 and 6 to the ConfigMap's value, empty when there is none. A family with no
    wanted value is left as it is. An entry that parses as neither family is never touched.
    """
    result = Plan()
    parsed = [(text, network(text)) for text in stored]
    for version in (4, 6):
        want = network(wanted.get(version, ""))
        if want is None or want.version != version:
            continue
        family = [(text, net) for text, net in parsed if net and net.version == version]
        stale = [text for text, net in family if net != want]
        absent = all(net != want for _, net in family)
        result.remove.extend(stale)
        if absent:
            result.add.append(entry(want))
        if stale or absent:
            before = ",".join(stale) or "<none>"
            result.changes.append(f"IPv{version} {before} -> {entry(want)}")
    return result


def age_seconds(stamp: str, now: datetime) -> float | None:
    """Seconds since `stamp`, an ISO UTC time, or None when it cannot be read."""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds()


def verdict(data: Mapping[str, str], changes: list[str]) -> tuple[int, str]:
    """`(exit code, summary)` once the allowlist is synced, by the rules in the docstring."""
    v4_error, v6_error = data.get("ipv4_error", ""), data.get("ipv6_error", "")
    faults = [
        f"IPv{n}: {error}"
        for n, error in ((4, v4_error), (6, v6_error))
        if error and not error.startswith(WAN_UNREACHABLE)
    ]
    done = "; ".join(changes) if changes else "home IPs unchanged"
    if faults:
        return 1, f"{'; '.join(faults)}; {done}"
    if v4_error:
        return 0, f"skipped: {v4_error}; {done}"
    return 0, f"home allowlist updated: {done}" if changes else done


def run(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def main(
    runner: Callable[[list[str]], str] = run,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    environ = os.environ if env is None else env
    now = now or datetime.now(UTC)
    configmap = json.loads(
        runner(
            [
                KUBECTL,
                "-n",
                NAMESPACE,
                "get",
                "configmap",
                environ["HOME_IPS_CONFIGMAP"],
                "-o",
                "json",
            ]
        )
    )
    data = configmap.get("data") or {}
    stale_after = float(environ["HOME_IPS_STALE_AFTER_S"])
    age = age_seconds(data.get("looked_up_at", ""), now)
    if age is None or age > stale_after:
        seen = "never" if age is None else f"{int(age)}s ago"
        print(
            f"home-ips discovery is stale: last lookup {seen}, past {int(stale_after)}s;"
            " the crowdsec-home-ips CronJob is not running"
        )
        return 1
    inspected = json.loads(
        runner([CSCLI, "allowlists", "inspect", ALLOWLIST, "-o", "json"]) or "{}"
    )
    stored = [
        item["value"] for item in inspected.get("items") or [] if item.get("value")
    ]
    decided = plan({4: data.get("ipv4", ""), 6: data.get("ipv6_prefix", "")}, stored)
    # Add before remove, so a failed call never leaves a family with no entry at all.
    for value in decided.add:
        runner([CSCLI, "allowlists", "add", ALLOWLIST, value, "-d", "home"])
    for value in decided.remove:
        runner([CSCLI, "allowlists", "remove", ALLOWLIST, value])
    code, line = verdict(data, decided.changes)
    print(line)
    return code


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()
        words = [Path(exc.cmd[0]).name, *exc.cmd[1:]]
        verb = " ".join([w for w in words if not w.startswith("-")][:3])
        print(
            f"{verb} failed rc={exc.returncode}: {tail[-1] if tail else 'no output'}",
            file=sys.stderr,
        )
        sys.exit(1)
