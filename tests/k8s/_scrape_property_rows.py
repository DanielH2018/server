"""Rendered-manifest property rows about how Prometheus and kubelet poll an exporter, and who reaches Prometheus.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re
from functools import cache
from urllib.parse import urlsplit

from lib import yaml_fast

from lib.estate import host_vars
from _k8s_render import pod_spec
from _property_table import Property

# `startupProbe` is included: a fast one against /metrics floods during every rollout.
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


def _node_exporter_liveness(role: str, tpl: str, doc: dict):
    if role != "node-exporter" or doc.get("kind") != "DaemonSet":
        return
    for container in pod_spec(doc).get("containers", []):
        if container.get("name") == "node-exporter":
            yield "node-exporter.livenessProbe", container.get("livenessProbe") or {}


def _metrics_probe_offence(role: str, probe: dict) -> str | None:
    path = str(probe["httpGet"].get("path", "/"))
    if not is_metrics_route(path):
        return None
    return (
        f"GETs {path}: the prober closes mid-body and the exporter logs one line per "
        "unwritten metric family. Probe a cheap path, or exec a client that reads the body"
    )


def _full_read_offence(role: str, probe: dict) -> str | None:
    command = (probe.get("exec") or {}).get("command") or []
    if not command:
        return "is not an exec probe, so nothing reads the whole /metrics body"
    if "/dev/null" not in command:
        return f"runs {command}, which does not read the body to /dev/null"
    if not any(is_metrics_route(urlsplit(arg).path) for arg in command if "://" in arg):
        return f"runs {command}, which does not fetch /metrics"
    return None


def _probe(path: str) -> dict:
    return {"httpGet": {"path": path, "port": 9100}, "periodSeconds": 300}


def _wget(url: str) -> dict:
    return {"exec": {"command": ["wget", "-q", "-T", "5", "-O", "/dev/null", url]}}


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
    # The Pi's own literal: reading the derived value would compare the render against itself.
    return host_vars("daniel-pi")["server_ip"]


def _scrape_jobs(role: str, tpl: str, doc: dict):
    if role != "observability" or doc.get("kind") != "ConfigMap":
        return
    config = doc.get("data", {}).get("prometheus.yml")
    if config is None:
        return
    for job in yaml_fast.safe_load(config)["scrape_configs"]:
        yield job["job_name"], job


def _targets_pi(target: str) -> bool:
    # A blackbox target carries a scheme, so `http://<pi>:51821` is as much the Pi as `<pi>:9100`.
    return target.split("://", 1)[-1].startswith(f"{pi_ip()}:")


def _timeout_offence(timeout: str | None, interval: str) -> str | None:
    if timeout is None:
        return "has no scrape_timeout, so it flaps at 10s"
    if _seconds(timeout) <= _DEFAULT_SCRAPE_TIMEOUT_S:
        return f"sets scrape_timeout {timeout}, no longer than the 10s default"
    if _seconds(timeout) > _seconds(interval):
        return f"sets scrape_timeout {timeout} above its {interval} interval"
    return None


def _scrape_timeout_offence(role: str, job: dict) -> str | None:
    """A Pi target's effective timeout is its own `__scrape_timeout__`, else the job's."""
    job_timeout = job.get("scrape_timeout")
    interval = job.get("scrape_interval", "1m")
    blocks = job.get("static_configs") or []
    pi_blocks = [b for b in blocks if any(map(_targets_pi, b.get("targets") or []))]
    for block in blocks:
        own = (block.get("labels") or {}).get("__scrape_timeout__")
        if block in pi_blocks:
            if problem := _timeout_offence(own or job_timeout, interval):
                return f"targets daniel-pi at {block['targets']} but {problem}"
        elif own is not None:
            return f"gives {block['targets']} a scrape_timeout ({own}); only Pi targets may"
    if job_timeout is not None and len(pi_blocks) != len(blocks):
        return f"overrides scrape_timeout ({job_timeout}); only the Pi jobs may"
    return None


def _job(
    target: str, timeout: str | None = None, target_timeout: str | None = None
) -> dict:
    block: dict = {"targets": [target]}
    if target_timeout is not None:
        block["labels"] = {"__scrape_timeout__": target_timeout}
    job = {"scrape_interval": "1m", "static_configs": [block]}
    if timeout is not None:
        job["scrape_timeout"] = timeout
    return job


_SOAK_PEER = {
    "namespaceSelector": {
        "matchLabels": {"kubernetes.io/metadata.name": "flux-system"}
    },
    "podSelector": {"matchLabels": {"app": "soak"}},
}


def _prometheus_callers(role: str, tpl: str, doc: dict):
    if (
        doc.get("kind") == "NetworkPolicy"
        and doc["metadata"]["name"] == "prometheus-callers"
    ):
        yield "prometheus-callers", doc["spec"]


def _soak_peer_offence(role: str, spec: dict) -> str | None:
    for rule in spec.get("ingress", []):
        if {"protocol": "TCP", "port": 9090} in rule.get(
            "ports", []
        ) and _SOAK_PEER in rule.get("from", []):
            return None
    return "no rule on TCP 9090 holds flux-system AND app: soak in one from item"


def _callers(*peers: dict, port: int = 9090) -> dict:
    return {
        "ingress": [{"from": list(peers), "ports": [{"protocol": "TCP", "port": port}]}]
    }


_PI_TARGET = f"{pi_ip()}:9100"
_PI_PROBE_TARGET = f"http://{pi_ip()}:51821"
_CLUSTER_TARGET = "longhorn-backend.longhorn-system.svc:9500"

SCRAPE_PROPERTIES = (
    Property(
        name="no-http-probe-on-a-metrics-route",
        reason=(
            "kubelet's HTTP prober reads a bounded prefix of the response and closes the "
            "connection. node_exporter's /metrics is about 260KB over 312 families, so every "
            "probe was cut off mid-body and the exporter logged about 280 `connection reset by "
            "peer` ERROR lines per probe: at one probe per 5 minutes per node, every line "
            "node-exporter wrote. Every signal around it reads healthy (pods Ready, probes "
            "passing, scrapes fine), and no period or CPU limit removes it. A query string does "
            "not make it cheap either. The census is every httpGet probe rendered, so a NEW "
            "workload with a /metrics probe fails here."
        ),
        select=_http_probes,
        offence=_metrics_probe_offence,
        red=("node-exporter", _probe("/metrics")),
        green=("node-exporter", _probe("/")),
        more_red=(
            ("node-exporter", _probe("/metrics/")),
            ("node-exporter", _probe("/metrics?collect[]=uname")),
        ),
        more_green=(
            ("node-exporter", _probe("/-/healthy")),
            ("node-exporter", _probe("/healthz")),
        ),
        min_matches=10,
    ),
    Property(
        name="node-exporter-keeps-a-metrics-probe",
        reason=(
            "The cheap fix for the metrics-probe flood is to point every probe at `/`. That "
            "passes no-http-probe-on-a-metrics-route while removing the only thing that restarts "
            "a node-exporter whose collector has hung: a hung collector still answers `/`. The "
            "liveness probe therefore execs busybox wget, which reads the whole /metrics body "
            "into /dev/null, so the render runs to completion and nothing resets."
        ),
        select=_node_exporter_liveness,
        offence=_full_read_offence,
        red=("node-exporter", _probe("/metrics")),
        green=("node-exporter", _wget("http://127.0.0.1:9100/metrics")),
        more_red=(
            ("node-exporter", _wget("http://127.0.0.1:9100/")),
            (
                "node-exporter",
                {"exec": {"command": ["wget", "-q", "http://127.0.0.1:9100/metrics"]}},
            ),
        ),
        must_find=frozenset({"node-exporter.livenessProbe"}),
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
            "to surface, so every other job keeps the default. A blackbox job that probes one Pi "
            "target among cluster ones gives that target alone its budget through the "
            "per-target `__scrape_timeout__` label: wg-easy's http probe flapped at the 10s "
            "default (#8252)."
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
            ("observability", _job(_PI_PROBE_TARGET)),
            ("observability", _job(_PI_PROBE_TARGET, target_timeout="10s")),
            ("observability", _job(_CLUSTER_TARGET, target_timeout="30s")),
        ),
        more_green=(
            ("observability", _job(_CLUSTER_TARGET)),
            ("observability", _job(_PI_PROBE_TARGET, target_timeout="30s")),
        ),
        min_matches=10,
        must_find=frozenset(
            {"node-pi", "alloy-pi", "dockerd-pi", "containerd-pi", "blackbox-extra"}
        ),
    ),
    Property(
        name="prometheus-admits-the-soak-job",
        reason=(
            "The soak Job runs in flux-system and reads M2's homelab:pod_service:info and "
            "the critical alerts from Prometheus on 9090 (ADR-0020). Its name moves with each "
            "revision's SHA, so the peer selects the "
            "pod label `app: soak`. The namespaceSelector and the podSelector must be sibling "
            "keys of ONE from item, so they AND. As two items they OR: the namespaceSelector "
            "alone admits every pod in flux-system, and the podSelector alone admits an "
            "`app: soak` pod in observability. Both shapes render a valid policy."
        ),
        select=_prometheus_callers,
        offence=_soak_peer_offence,
        red=(
            "netpol-baseline",
            _callers(
                {"namespaceSelector": _SOAK_PEER["namespaceSelector"]},
                {"podSelector": _SOAK_PEER["podSelector"]},
            ),
        ),
        green=("netpol-baseline", _callers(_SOAK_PEER)),
        more_red=(("netpol-baseline", _callers(_SOAK_PEER, port=9091)),),
        must_find=frozenset({"prometheus-callers"}),
    ),
)
