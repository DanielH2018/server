"""Tests for `probe.py usage`: route grouping, the memory flags and the PVC projection."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import core
from diagnostics.probe_lib import usage

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
    lines = usage.format_requests(series, NAMESPACES, 21.7 * 86400)
    assert "over the last 21.7d" in lines[0]
    assert lines[2].split() == ["karakeep", "3210", "10", "200", "3000", "9999"]


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
