#!/usr/bin/env python3
"""`service()`'s `namespace` argument places a Service, and omitting it keeps `k8s_namespace`.

WHY THIS EXISTS. observability's grafana, kube-state-metrics and otel-collector Services were
hand-written only because the macro hard-coded `k8s_namespace` (#3355). They now call it with
`namespace=k8s_observability_namespace`. A Service in the wrong namespace still parses and
schema-checks clean, and it applies cleanly too, beside a Deployment it can never select. So
the manifest validator cannot see a dropped argument, and this test reads the namespace itself.

Run: uv run pytest ansible/tests/k8s/test_service_macro_namespace.py
"""

from _k8s_render import rendered_docs

OBSERVABILITY_MACRO_SERVICES = {"grafana", "kube-state-metrics", "otel-collector"}


def _service_namespaces(role: str) -> dict[str, str]:
    return {
        doc["metadata"]["name"]: doc["metadata"]["namespace"]
        for r, _, doc in rendered_docs()
        if r == role and doc["kind"] == "Service"
    }


def test_observability_services_land_in_the_observability_namespace():
    """The three converted Services render into `k8s_observability_namespace`, not `homelab`."""
    namespaces = _service_namespaces("observability")
    assert OBSERVABILITY_MACRO_SERVICES <= namespaces.keys()
    assert {n: namespaces[n] for n in OBSERVABILITY_MACRO_SERVICES} == dict.fromkeys(
        OBSERVABILITY_MACRO_SERVICES, "observability"
    )


def test_a_caller_omitting_namespace_lands_in_k8s_namespace():
    """nut and karakeep pass no `namespace`, so their Services keep the default `homelab`."""
    assert _service_namespaces("nut") == {"nut": "homelab"}
    assert set(_service_namespaces("karakeep").values()) == {"homelab"}
