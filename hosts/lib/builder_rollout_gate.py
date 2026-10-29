"""The builder's staged-rollout gate: the canary's package first, the other hosts' after it reports.

Split from host_package_builder.py (H3.12), which inherits `RolloutGate`. The design is
docs/target-architecture/host/h3-pi-pilot.md, *Staged rollout*. A run whose changed set holds
more than one host package stages the canary's alone and opens a gate at that commit. While the
gate is open the builder builds no other host package. The gate closes when the operator's
root-owned `release` file names the gate's commit or a descendant, or when Prometheus reports
the canary at that commit or a descendant with `homelab_host_reconcile_ok` at 1. Prometheus
unreachable or silent leaves the gate open, which fails closed. Stdlib only, plus the
`host_lib` the builder already ships beside it.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from host_lib import log as host_log

RELEASE = "/etc/homelab-builder/release"
# The canary order (*Staged rollout*): daniel-box runs the builder, so it always comes last.
# DECIDED: daniel-pi leads this order at all hours, including its two 2h15m gaps (23:00 to 00:59
# and 04:00 to 05:59 UTC). A Pi package published at 23:08 holds the other host packages until
# the Pi's 01:05 fire and its run, which the operator accepted on 2026-10-29 (#6530). PR 6531
# passed the Pi over while its updater was in a gap, and the ruling reverted that skip, which
# had made the builder restate the Pi's drop-in schedule in a table of its own. The delay does
# not page because monitor-bridge derives its builder lag grace from the Pi's longest gap
# (ansible/filter_plugins/updater_lag_grace.py, rendered in monitor-bridge's env.yaml.j2).
CANARY_ORDER = ("daniel-pi", "daniel-server", "daniel-box")
APPLIED = "homelab_host_applied_info"
RECONCILE_OK = "homelab_host_reconcile_ok"
QUERY_TIMEOUT_S = 10
_SHA = re.compile(r"[0-9a-f]{7,40}")
_HOST = re.compile(r"[a-z0-9][a-z0-9-]*")


def log(message: str) -> None:
    host_log(message, tag="homelab-builder")


def host_of(package: str) -> str | None:
    """The host a rendered package directory serves, None for a shared package.

    `hosts/daniel-pi` serves daniel-pi. `packages/homelab-updater` and `packages/homelab-tools`
    serve every host, so they sit outside the gate (*Staged rollout*).
    """
    head, _, host = package.partition("/")
    return host if head == "hosts" and host else None


def admit(
    changed: list[str], gate: dict | None, chosen: str
) -> tuple[list[str], list[str], dict | None]:
    """Split `changed` into the packages to stage now and those the gate defers.

    Args:
        changed: the package directories whose render differs from the published one.
        gate: the open gate, `{"host": ..., "sha": ...}`, or None.
        chosen: the full SHA of the commit this run builds.

    Returns:
        (staged, deferred, the gate after this run). The gate moves to `chosen` whenever the
        canary's package is staged.
    """
    hosts = [p for p in changed if host_of(p)]
    if gate is None:
        if len(hosts) < 2:
            return changed, [], None
        order = [h for h in CANARY_ORDER if f"hosts/{h}" in hosts]
        # A host outside the order is last, and a name sort keeps the choice stable.
        canary = order[0] if order else host_of(sorted(hosts)[0])
        gate = {"host": canary, "sha": chosen}
    canary_package = f"hosts/{gate['host']}"
    staged = [p for p in changed if not host_of(p) or p == canary_package]
    deferred = [p for p in changed if host_of(p) and p != canary_package]
    if canary_package in staged:
        gate = {**gate, "sha": chosen}
    return staged, deferred, gate


def fetch_url(url: str, timeout: float) -> bytes:
    """The body of an HTTP GET of `url`."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


class RolloutGate:
    """The gate's reads, for `Builder` to inherit.

    The inheriting class supplies `git`, `state`, `config`, `release_path` and `prom_fetch`.
    """

    state: dict
    config: dict[str, str]
    release_path: Path
    prom_fetch: Callable[[str, float], bytes] | None

    def git(self, *args: str) -> str:
        raise NotImplementedError

    def close_gate(self) -> None:
        """Close the open gate when the release file or the canary's report says to."""
        gate = self.state.get("gate")
        if not gate:
            return
        if self.descends(gate["sha"], self.release_text()):
            why = f"{self.release_path} names it or a descendant"
        elif self.canary_reports(gate):
            why = f"{gate['host']} reports it at reconcile_ok 1"
        else:
            return
        log(f"rollout gate at {gate['sha'][:12]} closed: {why}")
        self.state["gate"] = None

    def note_rollout(self, deferred: list[str], gate: dict | None) -> None:
        """Record the gate and the deferred packages once a run has staged or settled."""
        self.state["gate"], self.state["deferred"] = gate, deferred
        if deferred and gate:
            log(
                f"rollout gate defers {', '.join(deferred)} until {gate['host']} reports"
            )

    def release_text(self) -> str:
        """The first word of the release file, or "" when it is absent or unreadable."""
        try:
            return (self.release_path.read_text().split() or [""])[0]
        except OSError:
            return ""

    def descends(self, sha: str, other: str) -> bool:
        """Whether `other`, a SHA prefix read from outside, is `sha` or descends from it.

        A commit the clone does not know is not a descendant, so the gate stays open.
        """
        if not _SHA.fullmatch(other):
            return False
        if sha.startswith(other):
            return True
        try:
            self.git("merge-base", "--is-ancestor", sha, other)
        except RuntimeError:
            return False
        return True

    def canary_reports(self, gate: dict) -> bool:
        host = gate["host"]
        if not _HOST.fullmatch(host):
            return False
        applied = self.prom(f'{APPLIED}{{host="{host}"}}')
        reconcile = self.prom(f'{RECONCILE_OK}{{host="{host}"}}')
        if applied is None or reconcile is None:
            log(
                "rollout gate stays open: Prometheus unreachable on loopback and ClusterIP"
            )
            return False
        return any(float(s["value"][1]) == 1 for s in reconcile) and any(
            self.descends(gate["sha"], s["metric"].get("sha", "")) for s in applied
        )

    def prom(self, promql: str) -> list[dict] | None:
        """The instant-vector result of `promql`, asking each PROMETHEUS_URLS entry in turn.

        None when none answers with a success. config.env carries both addresses, never the clone.
        """
        fetch: Callable[[str, float], bytes] = self.prom_fetch or fetch_url
        query = urllib.parse.urlencode({"query": promql})
        for base in self.config.get("PROMETHEUS_URLS", "").split():
            try:
                answer = json.loads(
                    fetch(f"{base}/api/v1/query?{query}", QUERY_TIMEOUT_S)
                )
                if answer["status"] == "success":
                    return answer["data"]["result"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log(f"{base} did not answer {promql}: {type(exc).__name__}: {exc}")
        return None
