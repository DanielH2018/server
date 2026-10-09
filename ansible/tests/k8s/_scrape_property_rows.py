"""Rendered-manifest property rows about how Prometheus and kubelet poll an exporter.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re
from functools import cache

from lib import yaml_fast

from _helpers import HOST_VARS, load_yaml
from _k8s_render import pod_spec
from _property_table import Property

# Below this period, a probe on a metrics route makes the exporter serialise its whole registry
# into a socket the prober closes. 300s is node-exporter's liveness period, chosen so the
# residual is ~36 lines/min against the 1,440 lines/min the 30s version produced.
MIN_METRICS_PROBE_PERIOD_S = 300

# `startupProbe` is included: it runs at its own period until it first succeeds, so a fast one
# against /metrics floods the same way during every rollout.
_PROBE_KEYS = ("livenessProbe", "readinessProbe", "startupProbe")


def is_metrics_route(path: str) -> bool:
    """True for the metrics endpoint whatever its query string.

    `?collect[]=<collector>` still drags the go_*/process_*/promhttp_* floor along, about 10KB,
    which sits within a couple of hundred bytes of the prober's read limit.
    """
    return path.split("?", 1)[0].rstrip("/") == "/metrics"


def _http_probes(role: str, tpl: str, doc: dict):
    spec = pod_spec(doc)
    if not spec:
        return
    workload = doc.get("metadata", {}).get("name", "?")
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        for key in _PROBE_KEYS:
            probe = container.get(key)
            if isinstance(probe, dict) and "httpGet" in probe:
                yield f"{workload}/{container.get('name', '?')}.{key}", probe


def _node_exporter_metrics_probes(role: str, tpl: str, doc: dict):
    if role != "node-exporter":
        return
    for key, probe in _http_probes(role, tpl, doc):
        if is_metrics_route(str(probe["httpGet"].get("path", "/"))):
            yield key, probe


def _metrics_probe_offence(role: str, probe: dict) -> str | None:
    path = str(probe["httpGet"].get("path", "/"))
    # kubelet's own default when periodSeconds is unset, which is what actually runs.
    period = int(probe.get("periodSeconds", 10))
    if not is_metrics_route(path) or period >= MIN_METRICS_PROBE_PERIOD_S:
        return None
    return (
        f"GETs {path} every {period}s: the prober closes mid-body and the exporter logs one "
        f"line per unwritten metric family. Use a cheap path, or probe every "
        f"{MIN_METRICS_PROBE_PERIOD_S}s or slower"
    )


def _probe(path: str, period: int | None = None) -> dict:
    probe: dict = {"httpGet": {"path": path, "port": 9100}}
    if period is not None:
        probe["periodSeconds"] = period
    return probe


# Prometheus' own default when a job sets none.
_DEFAULT_SCRAPE_TIMEOUT_S = 10
_DURATION = re.compile(r"^(\d+)(ms|s|m|h)$")
_UNIT_S = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def _seconds(duration: str) -> float:
    match = _DURATION.match(duration)
    assert match, f"unparseable Prometheus duration {duration!r}"
    return int(match.group(1)) * _UNIT_S[match.group(2)]


@cache
def pi_ip() -> str:
    # The Pi's own literal, not the k8s_pi_client_ip that all.yml derives from it through
    # hostvars (#3719). Reading the derived value would compare the render against itself, and
    # a derivation that resolved to None would still match.
    return load_yaml(HOST_VARS / "daniel-pi.yml")["server_ip"]


def _scrape_jobs(role: str, tpl: str, doc: dict):
    if role != "observability" or doc.get("kind") != "ConfigMap":
        return
    config = doc.get("data", {}).get("prometheus.yml")
    if config is None:
        return
    for job in yaml_fast.safe_load(config)["scrape_configs"]:
        yield job["job_name"], job


def _scrape_timeout_offence(role: str, job: dict) -> str | None:
    on_pi = any(
        target.startswith(f"{pi_ip()}:")
        for block in job.get("static_configs") or []
        for target in block.get("targets") or []
    )
    timeout = job.get("scrape_timeout")
    if not on_pi:
        if timeout is None:
            return None
        return f"overrides scrape_timeout ({timeout}); only the Pi jobs may"
    if timeout is None:
        return "targets daniel-pi but has no scrape_timeout, so it flaps at 10s"
    if _seconds(timeout) <= _DEFAULT_SCRAPE_TIMEOUT_S:
        return f"sets scrape_timeout {timeout}, no longer than the 10s default"
    interval = job.get("scrape_interval", "1m")
    if _seconds(timeout) > _seconds(interval):
        return f"sets scrape_timeout {timeout} above its {interval} interval"
    return None


def _job(target: str, timeout: str | None = None) -> dict:
    job = {"scrape_interval": "1m", "static_configs": [{"targets": [target]}]}
    if timeout is not None:
        job["scrape_timeout"] = timeout
    return job


_PI_TARGET = f"{pi_ip()}:9100"
_CLUSTER_TARGET = "longhorn-backend.longhorn-system.svc:9500"

SCRAPE_PROPERTIES = (
    Property(
        name="metrics-probes-are-slow",
        reason=(
            "kubelet's HTTP prober reads a bounded prefix of the response and closes the "
            "connection. node_exporter's /metrics is 250,165 bytes over 312 families, so every "
            "probe was cut off mid-body and the exporter logged about 180 `connection reset by "
            "peer` lines per probe: at 8 probes a minute, 97% of all k8s-namespace Loki ingest. "
            "Every signal around it reads healthy (pods Ready, probes passing, scrapes fine), and "
            "raising the CPU limit changes the line rate not at all. A probe on a metrics route "
            "may exist, since it is the only thing that detects a WEDGED collector, but it must "
            "run every 300s or slower. A query string does not make it cheap. The census is every "
            "httpGet probe rendered, so a NEW workload with a fast /metrics probe fails here."
        ),
        select=_http_probes,
        offence=_metrics_probe_offence,
        # node-exporter's readiness probe as it was when it caused the flood.
        red=("node-exporter", _probe("/metrics", 10)),
        green=("node-exporter", _probe("/metrics", MIN_METRICS_PROBE_PERIOD_S)),
        more_red=(
            ("node-exporter", _probe("/metrics")),
            ("node-exporter", _probe("/metrics/", 30)),
            ("node-exporter", _probe("/metrics?collect[]=uname", 30)),
            ("node-exporter", _probe("/metrics?collect[]=loadavg", 30)),
        ),
        more_green=(
            ("node-exporter", _probe("/", 10)),
            ("node-exporter", _probe("/-/healthy", 10)),
            ("node-exporter", _probe("/healthz")),
            ("node-exporter", _probe("/api/health", 5)),
        ),
        min_matches=10,
    ),
    Property(
        name="node-exporter-keeps-a-metrics-probe",
        reason=(
            "The cheap fix for the metrics-probe flood is to point every probe at `/`. That "
            "passes metrics-probes-are-slow while removing the only thing that restarts a "
            "node-exporter whose collector has hung: a hung collector still answers `/`. Slowing "
            "the metrics probe down is correct; deleting it is not. A selector that stops "
            "matching here is the failure this row exists for, not a retirement."
        ),
        select=_node_exporter_metrics_probes,
        offence=_metrics_probe_offence,
        red=("node-exporter", _probe("/metrics", 30)),
        green=("node-exporter", _probe("/metrics", MIN_METRICS_PROBE_PERIOD_S)),
    ),
    Property(
        name="pi-scrape-jobs-outlast-the-default-timeout",
        reason=(
            "daniel-pi is the one host outside the cluster, and it is contended: load5 of "
            "2.4-6.6 on four slow cores, CPU PSI around 30% `some`. A scrape landing in a "
            "contended stretch takes seconds rather than its 0.2-0.6s median, and one crossing "
            "Prometheus' 10s default reads as `up == 0`: a single-cycle DOWN on the Cluster "
            "Scrape Targets monitor with a healthy exporter behind it. Every job targeting "
            "`k8s_pi_client_ip` therefore sets a scrape_timeout above 10s and within its "
            "interval (Prometheus refuses the config otherwise). The override is deliberately "
            "not global: an in-cluster target that takes 30s is the failure the monitor exists "
            "to surface, so every other job keeps the default."
        ),
        select=_scrape_jobs,
        offence=_scrape_timeout_offence,
        # The shape a third Pi exporter lands in when copied from a cluster job.
        red=("observability", _job(_PI_TARGET)),
        green=("observability", _job(_PI_TARGET, "30s")),
        more_red=(
            ("observability", _job(_PI_TARGET, "10s")),
            ("observability", _job(_PI_TARGET, "2m")),
            ("observability", _job(_CLUSTER_TARGET, "30s")),
        ),
        more_green=(("observability", _job(_CLUSTER_TARGET)),),
        min_matches=10,
        must_find=frozenset({"node-pi", "alloy-pi", "dockerd-pi", "containerd-pi"}),
    ),
)
