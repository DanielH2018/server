"""The rendered Alloy drops of the Flux controllers' and the CrowdSec LAPI's routine info lines.

A drop fails silently in both directions, so each case is a real line shape that must drop or
must ship. Addresses and names in the lines are documentation values.
"""

import pytest

from _alloy_pods_pipeline import dropped_by

_FLUX = ("manager", "flux-system")
_CROWDSEC = ("crowdsec", "homelab")

_KUSTOMIZATION = (
    '"controller":"kustomization","controllerGroup":"kustomize.toolkit.fluxcd.io",'
    '"controllerKind":"Kustomization","Kustomization":{"name":"soak","namespace":"flux-system"},'
    '"namespace":"flux-system","name":"soak","reconcileID":"ed43c7ac-1c5e-447c-8889-1cfda51fc092"'
)
_EVENT_OBJECT = (
    '"eventInvolvedObject":{"kind":"Kustomization","namespace":"flux-system","name":"soak",'
    '"uid":"644673c7-a25c-4650-8526-73c5f557db04","apiVersion":"kustomize.toolkit.fluxcd.io/v1",'
    '"resourceVersion":"55382812"}'
)


def _flux(msg: str, level: str = "info", tail: str = _KUSTOMIZATION) -> str:
    return f'{{"level":"{level}","ts":"2026-11-19T13:52:21.151Z","msg":"{msg}",{tail}}}'


def _event(message: str) -> str:
    return (
        '{"level":"info","ts":"2026-11-19T13:52:21.152Z","logger":"event-server",'
        f'"msg":"dispatching event",{_EVENT_OBJECT},"message":"{message}"}}'
    )


def _apply(output: str) -> str:
    return _flux(
        "server-side apply completed", tail=f'{_KUSTOMIZATION},"output":{{{output}}}'
    )


def _lapi(request: str, status: int) -> str:
    return (
        'time="2026-11-19T07:45:01-06:00" level=info msg="10.42.0.238 - '
        f'[Thu, 19 Nov 2026 07:45:01 CST] \\"{request} HTTP/1.1 {status} 34.2ms '
        '\\"crowdsec/v1.8.2-3d369d64-docker\\" \\"" module=lapi'
    )


def _waf_skip(level: str = "info") -> str:
    return (
        f'time="2026-11-19T07:58:46-06:00" level={level} msg="203.0.113.7 is allowlisted by '
        "203.0.113.7 from remote-ips (authenticated session seen 2026-11-16), skipping WAF "
        'processing" module=acquisition.appsec name=k8s-traefik-appsec '
        "request_uuid=2a17cbdb-d681-445f-8b10-bb2c709d3d88 type=appsec"
    )


DROPPED = {
    "flux-reconciliation-finished": (
        _FLUX,
        _flux("Reconciliation finished in 168.879179ms, next run in 10m0s"),
    ),
    "flux-artifact-up-to-date": (
        _FLUX,
        _flux(
            "artifact up-to-date with remote revision: 'prod@sha256:b47a1f118ed9'",
            tail='"controller":"ocirepository","name":"homelab"',
        ),
    ),
    "flux-dependencies-ready": (
        _FLUX,
        _flux("All dependencies are ready, proceeding with reconciliation"),
    ),
    "flux-dependency-wait": (
        _FLUX,
        _flux("Dependencies do not meet ready condition, retrying in 30s"),
    ),
    "flux-commit-status-skip": (
        _FLUX,
        _flux(
            "event dropped for commit status providers due to missing commit metadata key",
            tail=f'"logger":"event-server",{_EVENT_OBJECT},"alerts":["flux-system/commit-status"]',
        ),
    ),
    "flux-dispatched-reconciliation-finished": (
        _FLUX,
        _event("Reconciliation finished in 160.739449ms, next run in 10m0s"),
    ),
    "flux-dispatched-dependency-wait": (
        _FLUX,
        _event("Dependencies do not meet ready condition, retrying in 30s"),
    ),
    "flux-unchanged-apply": (
        _FLUX,
        _apply(
            '"ClusterRole/soak":"unchanged","ConfigMap/flux-system/soak-60c68bc0":"unchanged"'
        ),
    ),
    "crowdsec-allowlisted-waf-skip": (_CROWDSEC, _waf_skip()),
    "crowdsec-watcher-login": (_CROWDSEC, _lapi("POST /v1/watchers/login", 200)),
    "crowdsec-usage-metrics": (_CROWDSEC, _lapi("POST /v1/usage-metrics", 201)),
}

SHIPPED = {
    "flux-apply-that-created-an-object": (
        _FLUX,
        _apply(
            '"ClusterRole/soak":"unchanged","Job/flux-system/soak-c7b48189":"created"'
        ),
    ),
    "flux-podsecurity-warning": (
        _FLUX,
        _flux(
            'would violate PodSecurity \\"baseline:latest\\": hostPort (container \\"x\\")'
        ),
    ),
    "flux-error": (
        _FLUX,
        _flux(
            "Failed to watch",
            level="error",
            tail='"error":"failed to list *v1.OCIRepository: apiserver not ready"',
        ),
    ),
    "flux-error-reconciliation-finished": (
        _FLUX,
        _flux("Reconciliation finished in 1.2s, next run in 10m0s", level="error"),
    ),
    "flux-dispatched-health-check-failure": (
        _FLUX,
        _event(
            "Health check failed after 5m0s: timeout waiting for: [Deployment/homelab/x]"
        ),
    ),
    "flux-reconciliation-line-from-another-namespace": (
        ("manager", "homelab"),
        _flux("Reconciliation finished in 168.879179ms, next run in 10m0s"),
    ),
    "crowdsec-ban": (
        _CROWDSEC,
        'time="2026-11-19T06:26:30-06:00" level=info msg="(k8s-traefik-agent/crowdsec) '
        "crowdsecurity/http-probing by ip 198.51.100.9 (US/8075) : 4h ban on Ip "
        '198.51.100.9" module=db',
    ),
    "crowdsec-waf-block": (
        _CROWDSEC,
        'time="2026-11-19T07:50:47-06:00" level=info msg="WAF block: '
        'crowdsecurity/vpatch-git-config from 198.51.100.9 (10.42.0.238)"',
    ),
    "crowdsec-warning": (
        _CROWDSEC,
        'time="2026-11-19T07:14:50-06:00" level=warning msg="sqlite is not using WAL mode, '
        'LAPI might become unresponsive when inserting the community blocklist"',
    ),
    "crowdsec-refused-login": (_CROWDSEC, _lapi("POST /v1/watchers/login", 403)),
    "crowdsec-waf-skip-at-warning": (_CROWDSEC, _waf_skip(level="warning")),
    "crowdsec-agent-sidecar-login": (
        ("crowdsec-agent", "homelab"),
        _lapi("POST /v1/watchers/login", 200),
    ),
}


@pytest.mark.parametrize(("source", "line"), DROPPED.values(), ids=DROPPED.keys())
def test_a_routine_info_line_is_dropped(source, line):
    container, namespace = source
    assert dropped_by(container, line, namespace) is not None


@pytest.mark.parametrize(("source", "line"), SHIPPED.values(), ids=SHIPPED.keys())
def test_a_change_a_warning_or_a_failure_still_ships(source, line):
    container, namespace = source
    assert dropped_by(container, line, namespace) is None
