"""Tests for `probe.py usage`: route grouping, the memory and CPU flags and the PVC projection."""

import re

import pytest


from diagnostics.probe_lib import core
from diagnostics.probe_lib import usage
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability/files/rules/container-checks.yaml"

NAMESPACES = {"homelab", "longhorn-system", "observability"}
HASH = "0123456789abcdef0123"


def _series(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


@pytest.mark.parametrize(
    "label, expected",
    [
        (f"homelab-sonarr-{HASH}@kubernetescrd", ("sonarr", "lan")),
        (f"homelab-sonarr-public-{HASH}@kubernetescrd", ("sonarr", "public")),
        (f"homelab-karakeep-public-api-trpc-{HASH}@kubernetescrd", ("karakeep", "api")),
        (
            f"homelab-loki-homelab-push-monitoring-{HASH}@kubernetescrd",
            ("loki-homelab", "monitoring"),
        ),
        (
            f"observability-alertmanager-read-{HASH}@kubernetescrd",
            ("alertmanager", "api"),
        ),
        (
            f"longhorn-system-longhorn-frontend-{HASH}@kubernetescrd",
            ("longhorn-frontend", "lan"),
        ),
        ("livesync-couchdb@file", ("livesync-couchdb", "lan")),
        ("prometheus@internal", None),
    ],
)
def test_a_route_label_names_its_service_and_route_kind(label, expected):
    assert usage.route_service(label, NAMESPACES) == expected


def test_a_services_requests_sum_its_lan_public_and_api_routes_but_not_monitoring():
    series = [
        _series(10, service=f"homelab-karakeep-{HASH}@kubernetescrd"),
        _series(200, service=f"homelab-karakeep-public-{HASH}@kubernetescrd"),
        _series(3000, service=f"homelab-karakeep-public-api-v1-{HASH}@kubernetescrd"),
        _series(9999, service=f"homelab-karakeep-monitoring-{HASH}@kubernetescrd"),
    ]
    routes = [("homelab", "karakeep"), ("homelab", "karakeep-public")]
    lines = usage.format_requests(series, routes, 21.7 * 86400)
    assert "over the last 21.7d" in lines[0]
    assert lines[2].split() == ["karakeep", "3210", "10", "200", "3000", "9999"]


def test_a_route_no_client_has_requested_still_lists_its_service_at_zero():
    series = [_series(5, service=f"homelab-sonarr-{HASH}@kubernetescrd")]
    routes = [("homelab", "sonarr"), ("homelab", "bento-pdf-public")]
    lines = usage.format_requests(series, routes, 86400)
    assert lines[0].startswith("Requests: 2 services, 1 with no request")
    assert lines[3].split() == ["bento-pdf", "0", "0", "0", "0", "0"]


@pytest.mark.parametrize(
    "pod, expected",
    [
        ("traefik-94979ff84-rtvkj", "traefik"),
        ("addon-job-reaper-29900745-xhcds", "addon-job-reaper"),
        ("scrutiny-collector-kgdqf", "scrutiny-collector"),
        ("pihole-2-0", "pihole-2"),
    ],
)
def test_a_pod_name_maps_to_its_workload(pod, expected):
    assert usage.workload(pod) == expected


def _memory(request, limit, p95s, show_all=False):
    labels = {"namespace": "homelab", "container": "app"}
    return usage.format_memory(
        [_series(v, pod=f"app-6b4f9c8d7-{n}", **labels) for n, v in p95s],
        [_series(request, pod="app-6b4f9c8d7-xk2vq", **labels)],
        [_series(limit, pod="app-6b4f9c8d7-xk2vq", **labels)],
        show_all=show_all,
    )


def test_a_container_whose_p95_nears_its_limit_is_flagged():
    mib = 2**20
    lines = _memory(200 * mib, 256 * mib, [("xk2vq", 230 * mib)])
    assert lines[2].endswith("near limit")
    assert lines[2].startswith("  homelab/app/app")


def test_the_p95_is_the_highest_of_any_pod_of_the_workload():
    mib = 2**20
    lines = _memory(64 * mib, 512 * mib, [("xk2vq", 60 * mib), ("bbbbb", 200 * mib)])
    assert "200Mi" in lines[2] and lines[2].endswith("request off")


def test_a_container_matching_its_request_is_counted_but_listed_only_under_all():
    mib = 2**20
    lines = _memory(100 * mib, 512 * mib, [("xk2vq", 110 * mib)])
    assert lines[0].startswith("Memory: 1 containers, 0 flagged")
    assert len(lines) == 2
    assert len(_memory(100 * mib, 512 * mib, [("xk2vq", 110 * mib)], True)) == 3


def _cpu(containers, pod_throttled=True):
    """`format_cpu` over `{container: (request, limit, p95, throttle, cpu_psi, mem_psi)}`.

    Every container is in one pod, which met the alert's condition when `pod_throttled`.
    """
    labels = {"namespace": "homelab", "pod": "app-6b4f9c8d7-xk2vq"}
    columns = [[] for _ in range(6)]
    for container, cells in containers.items():
        for column, value in zip(columns, cells, strict=True):
            if value is not None:
                column.append(_series(value, container=container, **labels))
    request, limit, p95, throttle, cpu_psi, memory_psi = columns
    pods = [_series(0.4, **labels)] if pod_throttled else []
    return usage.format_cpu(p95, request, limit, throttle, pods, cpu_psi, memory_psi)


def test_a_container_throttled_and_stalled_is_listed_before_one_only_throttled():
    lines = _cpu(
        {
            "a-throttled": (0.1, 1.0, 0.1, 0.46, 0.02, 0.0),
            "b-stalled": (0.1, 2.0, 0.99, 0.99, 0.12, 0.08),
        }
    )
    assert lines[0].startswith("CPU: 2 containers, 2 flagged")
    assert lines[2].split()[1:] == [
        "0.10",
        "2.00",
        "0.99",
        "99.0%",
        "12.0%",
        "8.0%",
    ] + [
        "throttled,",
        "stalled",
    ]
    assert lines[3].startswith("  homelab/app/a-throttled") and lines[3].endswith(
        "  throttled"
    )


def test_a_pod_over_the_gates_marks_its_throttled_containers_but_not_an_unthrottled_one():
    lines = _cpu(
        {
            "a-sidecar": (0.05, 0.5, 0.03, 0.4, 0.0, 0.0),
            "b-app": (0.05, 0.5, 0.03, 0.4, 0.0, 0.0),
            "c-idle": (0.05, 0.5, 0.0, 0.0, 0.0, 0.0),
        }
    )
    assert lines[0].startswith("CPU: 3 containers, 2 flagged")
    assert [line.split()[0] for line in lines[2:]] == [
        "homelab/app/a-sidecar",
        "homelab/app/b-app",
    ]


def test_a_throttled_container_in_a_pod_under_the_gates_is_not_flagged():
    lines = _cpu({"app": (0.05, 0.5, 0.03, 0.4, 0.0, 0.0)}, pod_throttled=False)
    assert lines[0].startswith("CPU: 1 containers, 0 flagged")


def test_memory_pressure_alone_marks_a_container_stalled():
    lines = _cpu({"app": (0.1, 1.0, 0.1, None, 0.0, 0.3)})
    assert lines[2].endswith("30.0%  stalled")


def test_a_container_with_no_cpu_limit_is_counted_but_not_listed():
    lines = _cpu({"app": (0.1, None, 0.05, None, 0.01, 0.0)})
    assert lines[0].startswith("CPU: 1 containers, 0 flagged")
    assert len(lines) == 2


def test_a_cpu_p95_near_its_limit_is_flagged():
    lines = _cpu({"app": (0.1, 0.5, 0.45, None, 0.0, 0.0)})
    assert lines[2].split()[4] == "-" and lines[2].endswith("near limit")


def test_the_throttle_query_carries_both_gates_of_the_throttle_alert():
    [rule] = [
        r["expr"]
        for group in yaml_fast.safe_load(RULES.read_text())["groups"]
        for r in group["rules"]
        if r.get("alert") == "ContainerCPUThrottled"
    ]
    query = usage.THROTTLED_POD_QUERY

    def gates(expr):
        return sorted({float(n) for n in re.findall(r"> ([0-9.]+)", expr)})

    def rate_windows(expr):
        return set(re.findall(r"\[(\w+)\]", expr))

    def sums(expr):
        return set(re.findall(r"sum by \(([^)]*)\)", expr))

    assert gates(rule) == gates(query) == [usage.THROTTLED_CORES, usage.THROTTLED]
    assert rate_windows(rule) == rate_windows(query) == {usage.CPU_WINDOW}
    assert sums(rule) == sums(query) == {"namespace, pod"}


def test_a_pvc_filling_inside_the_horizon_is_listed_with_its_days_to_full():
    gib = 2**30
    pvc = {"namespace": "homelab", "persistentvolumeclaim": "loki-data"}
    quiet = {"namespace": "homelab", "persistentvolumeclaim": "quiet"}
    lines = usage.format_pvcs(
        [_series(50 * gib, **pvc), _series(1 * gib, **quiet)],
        [_series(100 * gib, **pvc), _series(10 * gib, **quiet)],
        [_series(2 * gib / 86400, **pvc), _series(0, **quiet)],
    )
    assert lines[0].startswith("PVCs: 2, 1 over 80% full or full inside 90 days")
    assert len(lines) == 3 and lines[2].split() == [
        "homelab/loki-data",
        "50.0%",
        "25",
    ]


def test_no_retention_span_is_unreadable_not_an_empty_report():
    with pytest.raises(core.Unreadable):
        usage.report(query=lambda promql: [])
