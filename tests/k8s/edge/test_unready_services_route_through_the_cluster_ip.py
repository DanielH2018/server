"""A Service that publishes unready addresses is routed through its cluster IP.

`publishNotReadyAddresses` keeps an unready pod's endpoint `ready`, which kube-proxy routes
to. Traefik picks pods itself and skips an endpoint that is not `serving`, so the field alone
does nothing for a route. On 2026-11-27 tft-assistant's page answered 503 while its crawler
container restarted, with the field set. A route to such a Service sets `nativeLB: true`, so
the request goes to the cluster IP and kube-proxy picks the endpoint.

The check joins two documents, a Service and each IngressRoute that names it, so it is a file
and not a row of the rendered-property table.

Run: uv run pytest tests/k8s/edge/test_unready_services_route_through_the_cluster_ip.py
"""

from collections.abc import Iterable

from _k8s_render import rendered_docs

# Subset, not equality: the census must still find the Service the rule was written for.
KNOWN_UNREADY_SERVICES = frozenset({"homelab/tft-assistant"})


def _key(doc: dict) -> str:
    return f"{doc['metadata']['namespace']}/{doc['metadata']['name']}"


def unready_services(docs: Iterable[dict]) -> set[str]:
    """Each `<namespace>/<name>` Service that publishes its unready addresses."""
    return {
        _key(doc)
        for doc in docs
        if doc.get("kind") == "Service"
        and doc.get("spec", {}).get("publishNotReadyAddresses")
    }


def routes_past_the_cluster_ip(docs: list[dict]) -> list[str]:
    """Each IngressRoute that sends requests for an unready-publishing Service to its pods."""
    unready = unready_services(docs)
    return sorted(
        f"{_key(doc)} routes to {namespace}/{service['name']} without nativeLB"
        for doc in docs
        if doc.get("kind") == "IngressRoute"
        for route in doc["spec"].get("routes", [])
        for service in route.get("services", [])
        for namespace in [service.get("namespace", doc["metadata"]["namespace"])]
        if f"{namespace}/{service['name']}" in unready and not service.get("nativeLB")
    )


def _service(publish: bool) -> dict:
    return {
        "kind": "Service",
        "metadata": {"name": "page", "namespace": "homelab"},
        "spec": {"publishNotReadyAddresses": publish},
    }


def _route(**service: object) -> dict:
    return {
        "kind": "IngressRoute",
        "metadata": {"name": "page", "namespace": "homelab"},
        "spec": {"routes": [{"services": [{"name": "page", "port": 8080, **service}]}]},
    }


def test_every_route_to_an_unready_publishing_service_uses_its_cluster_ip():
    # fact: k8s/roles/tft-assistant/CLAUDE.md#Notable
    docs = [doc for _, _, doc in rendered_docs()]

    assert unready_services(docs) >= KNOWN_UNREADY_SERVICES
    assert routes_past_the_cluster_ip(docs) == []


def test_a_route_to_the_pods_of_an_unready_publishing_service_is_flagged():
    assert routes_past_the_cluster_ip([_service(publish=True), _route()]) == [
        "homelab/page routes to homelab/page without nativeLB"
    ]


def test_a_route_through_the_cluster_ip_or_to_an_ordinary_service_is_clean():
    assert routes_past_the_cluster_ip([_service(True), _route(nativeLB=True)]) == []
    assert routes_past_the_cluster_ip([_service(False), _route()]) == []
