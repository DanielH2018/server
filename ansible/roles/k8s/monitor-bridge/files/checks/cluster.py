"""Cluster checks for monitor-bridge.

Covers the Prometheus gate; the cAdvisor trio is observability's `container-checks.yaml`, the
Kubernetes workload arms are the k8s-workloads rules, and the Traefik edge is observability's
`edge.yaml`.

Slice 6 of the check.py split. Reads config as `cfg.X` and every query through the `src` argument
(`bridge.sources.Sources`), so a test hands in a fake `src`.
Rule and enforcement: bridge/config.py's header.
"""

from bridge.config import Config
from bridge.sources import Sources


def check_prometheus(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Is Prometheus itself reachable and answering queries?

    A trivial `vector(1)` instant query returns 1.0 whenever Prometheus is up; if it's
    down/unreachable prom_scalar raises (connection error) and run_once renders this monitor
    `down` with the error. This is the single root-cause signal for the prom-dependent checks:
    run_once probes it FIRST each cycle and, when it's down, SUPPRESSES the metric checks (which
    would otherwise all fail at once — one outage, a storm of identical pages) so only this
    monitor alerts. A single scrape target being down (Prometheus up, one exporter gone) still
    surfaces separately as ScrapeTargetDown — a distinct condition from this one.
    """
    val = src.prom_scalar("vector(1)")
    if val is None:
        return False, "Prometheus answered but returned no data for vector(1)"
    return True, "Prometheus reachable"
