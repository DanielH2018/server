"""The cluster-facing half of monitor-bridge's configuration.

The origin pin, the scrape-target coverage floors, the kube-state-metrics
workload floors.

Most of these are FLOORS rather than limits, and the comments beside them say what an absent
series would otherwise read as. Field justifications sit beside the declarations, env var names
beside the reads, and the values in `templates/config/env.yaml.j2`. Composed into `Config` by `bridge/config.py`; imports nothing
from it.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterConfig:
    """The origin pin and the coverage floors."""

    PROM_ORIGIN: str


def cluster_config(
    _env: Callable[..., str],
    _int: Callable[..., int],
    _num: Callable[..., float],
) -> ClusterConfig:
    """The cluster fields, read through the parsers `load_config` built over its environment."""
    return ClusterConfig(
        # Which estate the host-health checks mean, when one Prometheus holds two (slice 3, B5).
        #
        # Since B3 the cluster Prometheus carries daniel-server's whole TSDB alongside its own,
        # tagged with `origin` (Prometheus external_labels). Three metric families genuinely
        # exist on BOTH sides — measured 2026-08-07: container_start_time_seconds (99
        # cluster-native / 53 here), container_memory_failcnt and the container_cpu_cfs_* pair,
        # plus `up` (5 / 11). So the moment PROMETHEUS_URL points at the cluster, a check on
        # those families silently widens from "daniel-server's containers" to "every
        # container in the homelab", and would start naming k8s pods as offenders. The
        # PROM_DEPENDENT checks on node-exporter series are pinned. Disk and memory are the two
        # exceptions: they are HOST checks, and pinning them to one origin would leave the other
        # host's root disk and memory unwatched — so they group `by (origin)` and report the
        # worst, covering both.
        #
        # `{name!=""}` does NOT already scope this, which is the obvious assumption and a wrong
        # one: the kubelet's cAdvisor emits `name` too, so 99 cluster-native series survive that
        # filter.
        #
        # A CONSTANT with an override, since 2026-09-28 (#2825). It used to be derived from
        # whether PROMETHEUS_URL and CLUSTER_PROMETHEUS_URL named one instance, because the pin
        # is required when reading the cluster copy and WRONG when reading the Docker instance
        # — whose own storage had no `origin` label at all, external_labels being applied on
        # remote-write and never to local queries. That Docker instance retired 2026-08-14, so
        # there is one Prometheus left and it is the one that carries the label. A derivation
        # with one reachable branch is a derivation in name only.
        #
        # It is NOT re-derived from the URL's shape. `probe.py` already reaches this same
        # Prometheus over an IngressRoute rather than the Service name, so a hostname test
        # would answer "does this URL look in-cluster" when the question is "does this
        # Prometheus carry `origin`" — and the wrong answer selects the empty vector, which
        # every check reading it decodes as healthy. The default therefore pins, and a third
        # estate whose series carry no `origin` sets PROM_ORIGIN="" explicitly.
        PROM_ORIGIN=_env("PROM_ORIGIN", 'origin="daniel-server"'),
    )
