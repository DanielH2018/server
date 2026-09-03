#!/usr/bin/env python3
"""Watch the TLS leaf cert of every PUBLIC hostname this homelab routes, over Traefik.

Fetches the leaf certificate Traefik serves for each public k8s route and warns when one
expires within 14 days. Notifies only on a transition into or out of that window (see
``scripts.lib.watcher.run_watcher``), not on every run.

WHICH HOSTNAMES. Derived from the same source ``scripts/docs/service_catalog.py`` uses --
``containers_list`` entries for a k8s role whose ``ingressroute.yaml.j2`` reaches PUBLIC per
``scripts/docs/route_facts.py`` -- never hand-listed, so a newly-public service is picked up
without editing this file.

WHERE THE FETCH CONNECTS. Not the public hostname's own DNS answer: that name is
Cloudflare-proxied, so a normal connection would read the Cloudflare edge cert, not
Traefik's origin cert, and repeatedly connecting to the public name risks tripping the
homelab's own CrowdSec edge (project memory: burst-testing a public hostname self-bans over
it). Instead this connects to the cluster's own MetalLB ingress VIP with the public hostname
as the TLS SNI -- the same ``--resolve``-pin idiom ``scripts/diagnostics/probe_lib/core.py``
uses for `.local.` names -- so it reads the real origin leaf over the LAN only.

Run:
    uv run python scripts/watchers/cert_expiry.py --dry-run   # print the table, no notify
    uv run python scripts/watchers/cert_expiry.py             # fetch, notify on a state
                                                                # change, ping the healthcheck

Env (both optional -- a watcher with neither just logs):
    CERT_EXPIRY_DISCORD_WEBHOOK_URL, CERT_EXPIRY_HEALTHCHECK_URL

Tests: uv run pytest scripts/watchers/tests
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# A directly-invoked script gets only its OWN directory on sys.path (repo-root CLAUDE.md,
# "Directory Structure"); every sibling package below needs its own bootstrap entry.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "docs"))

from diagnostics.probe_lib.core import metallb_vip, sops_extract
from lib.render_guard import containers_entries, host_files
from lib.repo_paths import ALL_VARS, HOST_VARS, K8S_ROLES
from lib.watcher import Watcher, configure_logging, load_state, run_watcher
from route_facts import PUBLIC, reachability

logger = configure_logging("cert-expiry")

THRESHOLD_DAYS = 14
STATE_PATH = Path.home() / ".local" / "state" / "homelab-watchers" / "cert-expiry.json"

# The non-vacuity floor for the hostname census below: services this repo has routed
# publicly for a long time, so their absence means the derivation broke rather than that
# the fleet shrank. Not the full set -- see scripts/watchers/tests/test_cert_expiry.py.
KNOWN_PUBLIC_LABELS = frozenset(
    {"auth", "home-assistant", "jellyfin", "sonarr", "uptime-kuma"}
)


def public_hostname_labels(
    k8s_roles: Path = K8S_ROLES,
    host_vars: Path = HOST_VARS,
    all_vars: Path = ALL_VARS,
) -> list[str]:
    """Every k8s route label reachable on the PUBLIC hostname, sorted and deduped.

    A role's route label is its ``containers_list`` ``hostname`` override, or its name --
    the same fallback ``ingressroute.yml.j2``'s own macro call uses (see
    ``scripts/docs/service_catalog.py::k8s_route``).
    """
    labels: set[str] = set()
    for host_vars_file in host_files(host_vars):
        for entry in containers_entries(host_vars_file):
            if entry.get("platform", "docker") != "k8s":
                continue
            role_dir = k8s_roles / entry["name"]
            if not (role_dir / "templates" / "ingressroute.yaml.j2").is_file():
                continue
            if reachability(role_dir, all_vars) != PUBLIC:
                continue
            labels.add(entry.get("hostname") or entry["name"])
    return sorted(labels)


def expiring_soon(
    not_after: datetime, now: datetime, threshold_days: int = THRESHOLD_DAYS
) -> bool:
    """Whether `not_after` is within `threshold_days` of `now` (or already past)."""
    return (not_after - now).days <= threshold_days


def fetch_leaf_not_after(
    hostname: str, vip: str, port: int = 443, timeout: float = 10
) -> datetime:
    """Open a TLS session against `vip` with SNI=`hostname` and return the served leaf's expiry."""
    ctx = ssl.create_default_context()
    # TLS 1.0/1.1 stay negotiable under the default context; the edge only speaks 1.2+.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((vip, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
            cert = tls.getpeercert()
    if cert is None:
        raise ssl.SSLError(f"no peer certificate presented for {hostname}")
    not_after = cert["notAfter"]  # e.g. "Sep  3 12:00:00 2026 GMT"
    if not isinstance(not_after, str):
        raise ssl.SSLError(f"unexpected notAfter shape for {hostname}: {not_after!r}")
    return datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc
    )


def build_current(
    labels: list[str],
    domain: str,
    vip: str,
    now: datetime,
    *,
    fetch: Callable[[str, str], datetime] = fetch_leaf_not_after,
    previous: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch every host's leaf expiry into the shape ``check_expiring`` compares.

    A per-host fetch failure is logged, and that host's entry is carried forward from
    `previous` if there is one -- a transient blip must not read as a renewed cert, nor
    silently drop a host from state until it flaps back. If EVERY fetch in the run fails,
    that is not a quiet all-clear: it raises, so the caller's `run_watcher` treats the run
    as failed and pings the healthcheck's `/fail` instead of silently re-saving stale data
    forever. This checks whether anything was actually FETCHED this run, not whether
    `current` ends up non-empty -- a VIP that moved or a CA-store change can fail every
    fetch while `previous` still carries forward every host, which would otherwise leave
    `current` non-empty and the failure permanently invisible.
    """
    previous = previous or {}
    current: dict[str, dict[str, Any]] = {}
    fetched_any = False
    for label in labels:
        hostname = f"{label}.{domain}"
        try:
            not_after = fetch(hostname, vip)
        except (OSError, ssl.SSLError) as exc:
            logger.warning("cert fetch failed for %s: %s", hostname, exc)
            if hostname in previous:
                current[hostname] = previous[hostname]
            continue
        fetched_any = True
        current[hostname] = {
            "not_after": not_after.isoformat(),
            "expiring": expiring_soon(not_after, now),
        }
    if labels and not fetched_any:
        raise RuntimeError(f"cert fetch failed for all {len(labels)} public hostnames")
    return current


def check_expiring(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> str | None:
    """Notify only on a transition into or out of the expiry window -- not on every run."""
    prev = previous or {}
    newly = sorted(
        host
        for host, c in current.items()
        if c.get("expiring") and not prev.get(host, {}).get("expiring")
    )
    resolved = sorted(
        host
        for host, c in current.items()
        if not c.get("expiring") and prev.get(host, {}).get("expiring")
    )
    if not newly and not resolved:
        return None
    parts = []
    if newly:
        parts.append(
            f"TLS cert expiring within {THRESHOLD_DAYS} days: " + ", ".join(newly)
        )
    if resolved:
        parts.append(
            "TLS cert renewed (no longer expiring soon): " + ", ".join(resolved)
        )
    return "; ".join(parts)


def print_table(current: dict[str, Any]) -> None:
    for hostname in sorted(current):
        c = current[hostname]
        flag = "EXPIRING" if c.get("expiring") else "ok"
        print(f"{hostname:45s} {c.get('not_after', 'unknown'):30s} {flag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the table and exit; never notify"
    )
    args = parser.parse_args(argv)

    labels = public_hostname_labels()
    domain = sops_extract("domain")
    vip = metallb_vip()
    now = datetime.now(timezone.utc)

    if args.dry_run:
        previous = load_state(STATE_PATH)
        print_table(build_current(labels, domain, vip, now, previous=previous))
        return 0

    watcher = Watcher(
        name="cert-expiry",
        state_path=STATE_PATH,
        # `previous` is loaded once here (not inside the closure) so a per-host fetch
        # failure can carry that host's last-known entry forward -- see build_current's
        # docstring. `run_watcher` also loads it, from the same unmodified file, to decide
        # whether `check_expiring`'s verdict is a transition worth notifying.
        fetch=lambda: build_current(
            labels, domain, vip, now, previous=load_state(STATE_PATH)
        ),
        check=check_expiring,
        logger=logger,
        webhook_url=os.environ.get("CERT_EXPIRY_DISCORD_WEBHOOK_URL"),
        healthcheck_url=os.environ.get("CERT_EXPIRY_HEALTHCHECK_URL"),
    )
    return run_watcher(watcher)


if __name__ == "__main__":
    raise SystemExit(main())
