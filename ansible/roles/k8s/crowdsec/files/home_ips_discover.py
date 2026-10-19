#!/usr/bin/env python3
"""Look up the home public IPv4 address and IPv6 /64, and write them into one ConfigMap.

The `crowdsec-home-ips` CronJob runs this on daniel-box every few minutes with `hostNetwork:
true`, because the pod network is IPv4-only and only the host's own network namespace holds
the home IPv6 /64 (the operator's ruling of 2026-10-18 on #5701, recorded in the H2 plan's
*Open questions* 2). It runs no `cscli` and execs into nothing. The `crowdsec-jobs` sidecar of
the LAPI pod reads the ConfigMap and syncs the `home-ips` allowlist from it
(`home_allowlist.py`).

- **IPv4** is the single NAT'd home address, so it comes from ipify, as the host cron's did. A
  value that is not a public IPv4 address counts as a failed lookup.
- **IPv6** has no NAT: every device is routable and privacy extensions rotate addresses
  inside the prefix, so the allowlist holds the /64, not an address. It comes from the host's
  global addresses in `/proc/net/if_inet6`, which a host-network pod reads from the host's
  namespace. Link-local, ULA and other non-global addresses are skipped, and so are
  tentative, deprecated and DAD-failed ones. Two distinct /64s count as a failed lookup,
  because the job cannot tell which one the home LAN egresses from.

**A failed or empty lookup for one family leaves that family's stored value in place.** The
patch then carries only the family's error field, never its value, so the sidecar never sees
an empty value where a good one was. An empty IPv6 lookup, a host with no global address, is
not an error, and also leaves the stored /64 alone. When ipify fails, the job probes the
`WAN_PROBE_URLS` endpoints, and when none answers it marks the error `WAN unreachable`, so the
sidecar can report a skip rather than a fault (#2793).

The ConfigMap's `data` has one writer, this job. The role renders the object with no `data`
keys (`templates/home-ips-configmap.yaml.j2`), so neither Ansible's apply nor a later Flux
apply owns a field this job writes. Each run writes, as a JSON merge patch:

    ipv4, ipv6_prefix   the looked-up value, only on a good lookup
    ipv4_error, ipv6_error   empty on a good lookup, else why it failed
    looked_up_at        the run's UTC time, on every run; the sidecar pages when it is stale

It talks to the API server with the pod's ServiceAccount token, which may only `patch` this one
ConfigMap, and the API server's CA reaches `urlopen` through `SSL_CERT_FILE`, which the
CronJob sets. Every connection goes through `flux_job_connect`.

Usage:

    python3 /jobs/home_ips_discover.py

Inputs come from the environment: `HOME_IPS_NAMESPACE` and `HOME_IPS_CONFIGMAP` name the
ConfigMap, and `WAN_PROBE_URLS` is a space-separated list. Exit 0 means the ConfigMap was
written, whatever the lookups found, and the last stdout line says what they found. Exit 1
means the patch failed. `-h` and `--help` print this text and exit 0.
"""

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
IF_INET6 = "/proc/net/if_inet6"
IPIFY4 = "https://api4.ipify.org"
# The seconds each lookup or API call may take. `open_url` retries a refused connect on top.
CALL_TIMEOUT = 10
# A WAN probe gets no retry: it runs only on a path that has already failed.
WAN_PROBE_TIMEOUT = 5
# The `if_inet6` scope of a global address, and the flags that make one unusable
# (include/uapi/linux/if_addr.h: IFA_F_DADFAILED, IFA_F_DEPRECATED, IFA_F_TENTATIVE).
SCOPE_GLOBAL = 0x00
UNUSABLE_FLAGS = 0x08 | 0x20 | 0x40
WAN_UNREACHABLE = "WAN unreachable"


class Failed(Exception):
    """A lookup or a call failed; the message says which and why."""


def prefix64(address: str) -> str:
    """The /64 network holding `address`. Raises ValueError on anything but an IPv6 address."""
    return str(ipaddress.IPv6Network(f"{address}/64", strict=False))


def global_prefixes(table: str) -> list[str]:
    """The distinct /64s of the usable global addresses in an `/proc/net/if_inet6` body.

    Each line is `<32 hex digits> <ifindex> <prefix length> <scope> <flags> <name>`, every
    number in hex.
    """
    found = set()
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        raw, scope, flags = fields[0], int(fields[3], 16), int(fields[4], 16)
        if scope != SCOPE_GLOBAL or flags & UNUSABLE_FLAGS:
            continue
        address = ipaddress.IPv6Address(int(raw, 16))
        if address.is_global:
            found.add(prefix64(str(address)))
    return sorted(found)


def lookup_ipv6(read: Callable[[], str]) -> tuple[str, str]:
    """`(prefix, error)`: one is empty. Both are empty when the host has no global address."""
    try:
        prefixes = global_prefixes(read())
    except (OSError, ValueError) as err:
        return "", f"cannot read {IF_INET6}: {err}"
    if len(prefixes) > 1:
        return "", f"{len(prefixes)} distinct global /64s, so none is the home prefix"
    return (prefixes[0] if prefixes else ""), ""


def _fetch(url: str, opener: Callable, timeout: float, **kwargs) -> str:
    with opener(url, timeout=timeout, **kwargs) as response:
        return response.read().decode().strip()


def wan_reachable(urls: list[str], opener: Callable) -> bool:
    """True as soon as one probe endpoint answers 2xx, and False when every one failed."""
    for url in urls:
        try:
            _fetch(url, opener, WAN_PROBE_TIMEOUT, retry_seconds=0)
        except OSError, ValueError:
            continue
        return True
    return False


def lookup_ipv4(opener: Callable, probe_urls: list[str]) -> tuple[str, str]:
    """`(address, error)` from ipify: exactly one is empty."""
    try:
        value = _fetch(IPIFY4, opener, CALL_TIMEOUT)
    except (OSError, ValueError) as err:
        why = f"failed to resolve public IPv4 from ipify: {err}"
        if probe_urls and not wan_reachable(probe_urls, opener):
            why = f"{WAN_UNREACHABLE} — {why}"
        return "", why
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return "", f"invalid IPv4 from ipify: {value[:60]!r}"
    if not address.is_global:
        return "", f"ipify returned a non-public IPv4 address: {address}"
    return str(address), ""


# Each family's value key, its error key, and how a summary names it.
FAMILIES = (("ipv4", "ipv4_error", "IPv4"), ("ipv6_prefix", "ipv6_error", "IPv6"))


def plan(ipv4: tuple[str, str], ipv6: tuple[str, str], now: datetime) -> dict:
    """The merge patch's `data`: a family's value only on a good lookup, its error always."""
    data = {"looked_up_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    for (key, error_key, _), (value, error) in zip(FAMILIES, (ipv4, ipv6), strict=True):
        if value:
            data[key] = value
        data[error_key] = error
    return data


def summary(data: Mapping[str, str]) -> str:
    """One line naming what was written, with no address in it."""
    parts = []
    for key, error_key, label in FAMILIES:
        if data.get(error_key):
            parts.append(f"{label} kept the stored value: {data[error_key]}")
        elif data.get(key):
            parts.append(f"{label} written")
        else:
            parts.append(f"{label} none found, stored value kept")
    return f"home-ips at {data['looked_up_at']}: " + "; ".join(parts)


class Kube:
    """The API server, as one merge patch on one object.

    Attributes:
        base: The API server's URL.
        token: The ServiceAccount token sent as a bearer credential.
        opener: Opens a `Request`; `open_url` unless a test passes its own.
    """

    def __init__(self, base: str, token: str, opener: Callable = open_url):
        self.base = base
        self.token = token
        self.opener = opener

    def call(self, method: str, path: str, body: Mapping) -> tuple[int, dict]:
        """`method` on `path` with `body` as a JSON merge patch; the status and parsed reply."""
        request = Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/merge-patch+json",
            },
            method=method,
        )
        try:
            with self.opener(request, timeout=CALL_TIMEOUT) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, _parse(err.read())


def _parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def write(kube: Kube, namespace: str, name: str, data: Mapping[str, str]) -> None:
    """Merge `data` into the ConfigMap's `data`, leaving every key it does not name alone."""
    path = f"/api/v1/namespaces/{namespace}/configmaps/{name}"
    try:
        status, body = kube.call("PATCH", path, {"data": dict(data)})
    except OSError as err:
        raise Failed(f"patching ConfigMap {name} failed: {err}") from err
    if status != 200:
        raise Failed(
            f"patching ConfigMap {name} answered HTTP {status}: {body.get('message', '')}"
        )


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    kube: Kube | None = None,
    opener: Callable = open_url,
    read_if_inet6: Callable[[], str] = lambda: Path(IF_INET6).read_text(),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
    environ = os.environ if env is None else env
    if kube is None:
        kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    data = plan(
        lookup_ipv4(opener, environ.get("WAN_PROBE_URLS", "").split()),
        lookup_ipv6(read_if_inet6),
        now(),
    )
    try:
        write(kube, environ["HOME_IPS_NAMESPACE"], environ["HOME_IPS_CONFIGMAP"], data)
    except Failed as err:
        print(f"crowdsec-home-ips: {err}", file=sys.stderr)
        return 1
    print(f"crowdsec-home-ips: {summary(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
