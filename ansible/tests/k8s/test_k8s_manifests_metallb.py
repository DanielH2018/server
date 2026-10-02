"""MetalLB: the two address pools, and the annotation namespace the version still honours.

The ingress VIP must be a single address that is never auto-assigned, the general pool must
not contain it, and the narrowing must land before the ingress pool is created -- a moved
VIP fails silently, because every manifest stays valid. Service annotations must use the
`metallb.io` namespace, and the pinned MetalLB version must still be one that reads it.
"""

from lib import yaml_fast
from _k8s_render import rendered_docs

from _manifest_guards import ALL_VARS, K3S, K3S_DEFAULTS, _render


def _ip_to_int(addr: str) -> int:
    parts = [int(p) for p in addr.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _pool_docs() -> list[dict]:
    """IPAddressPool documents in FILE order — the order kubectl applies them in."""
    rendered = _render(
        K3S / "templates" / "metallb-pool.yaml.j2",
        k3s_metallb_ingress_vip=ALL_VARS["k3s_metallb_ingress_vip"],
        k3s_metallb_pool=yaml_fast.safe_load(
            (K3S / "defaults" / "main.yml").read_text()
        )["k3s_metallb_pool"],
    )
    docs = [d for d in yaml_fast.safe_load_all(rendered) if d]
    return [d for d in docs if d["kind"] == "IPAddressPool"]


def _pools() -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in _pool_docs()}


def test_ingress_pool_is_a_single_address_that_is_never_auto_assigned():
    """`autoAssign: false` is the whole reservation of the ingress address.

    Without it MetalLB hands the ingress address to whichever LoadBalancer Service asks first, and
    ingress moves — after that address is in DNS and in the router's port-forward.
    """
    ingress = _pools()["ingress-pool"]
    assert ingress["spec"]["autoAssign"] is False
    assert ingress["spec"]["addresses"] == [f"{ALL_VARS['k3s_metallb_ingress_vip']}/32"]


def test_general_pool_does_not_contain_the_ingress_vip():
    """A /32 reservation means nothing if the auto-assigning pool still covers the address."""
    start, end = _pools()["homelab-pool"]["spec"]["addresses"][0].split("-")
    vip = _ip_to_int(ALL_VARS["k3s_metallb_ingress_vip"])
    assert not (_ip_to_int(start) <= vip <= _ip_to_int(end))


def test_the_general_pool_narrows_before_the_ingress_pool_is_created():
    """The wide pool has to narrow before the ingress pool exists, or the apply fails.

    kubectl applies documents in file order and MetalLB's validating webhook rejects
    overlapping pools. Applying ingress-pool ahead of it fails with:

        CIDR "10.0.0.240/32" in pool "ingress-pool" overlaps with already
        defined CIDR "10.0.0.240/29"

    — homelab-pool still covered .240-.250 at validation time. Reordering the file is the whole
    fix, which is exactly why it is worth a guard: nothing about the YAML looks order-sensitive.
    """
    names = [d["metadata"]["name"] for d in _pool_docs()]
    assert names.index("homelab-pool") < names.index("ingress-pool")


# Every role whose rendered Service pins an address. A census that stops naming one of these
# is checking fewer Services and still passing, which is the shape the glob it replaced failed
# in: it read `service.yaml.j2` only, and jellyfin pins its LAN address in `service-lan.yaml.j2`.
PINNED_SERVICE_ROLES = frozenset({"jellyfin", "mosquitto", "pihole", "traefik"})


def metallb_service_annotations(docs) -> list[tuple[str, str, str]]:
    """(role, template name, annotation key) for every metallb annotation on a Service.

    Reads the rendered manifests rather than the templates (#3209), so a comment naming the
    deprecated prefix — traefik's template carries five lines of them — cannot be credited as
    an annotation, and a Service rendered from a template of any name is covered.
    """
    found = []
    for role, name, doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Service":
            continue
        annotations = (doc.get("metadata") or {}).get("annotations") or {}
        found.extend(
            (role, name, key) for key in sorted(annotations) if "metallb" in key
        )
    return found


def deprecated_annotations(docs) -> list[str]:
    """`<role>/<template>: <key>` for every Service annotation on the retired prefix."""
    return [
        f"{role}/{name}: {key}"
        for role, name, key in metallb_service_annotations(docs)
        if key.startswith("metallb.universe.tf/")
    ]


def test_metallb_service_annotations_use_the_metallb_io_namespace():
    """Service annotations moved to metallb.io/ in MetalLB v0.15; universe.tf/ is deprecated.

    v0.16.0 reads both prefixes (controller/service.go valueForAnnotation, metallb.io winning)
    but emits a `deprecatedAnnotation` Warning Event per Service on every reconcile for the
    old one.

    The hazard behind this guard: Kubernetes accepts any annotation key and MetalLB ignores
    unrecognised ones, so a wrong prefix is completely silent — the Service is created, an
    address is assigned from the auto-assign pool instead of the pinned one, and the deploy
    is green.
    """
    found = deprecated_annotations(rendered_docs())
    assert found == [], (
        "these Services use a deprecated metallb.universe.tf/ annotation — use metallb.io/: "
        f"{found}"
    )


def test_the_annotation_census_names_every_service_that_pins_an_address():
    """Guard the guard: a census that matched nothing would pass the test above."""
    roles = {role for role, _, _ in metallb_service_annotations(rendered_docs())}
    assert PINNED_SERVICE_ROLES <= roles, (
        f"the census no longer sees {sorted(PINNED_SERVICE_ROLES - roles)} — the render or "
        "the annotations moved"
    )


def test_a_deprecated_annotation_is_flagged():
    """The rejecting half, against a document that is not in the tree."""
    assert deprecated_annotations(
        [
            (
                "widget",
                "service.yaml.j2",
                {
                    "kind": "Service",
                    "metadata": {
                        "annotations": {
                            "metallb.universe.tf/loadBalancerIPs": "10.0.0.249"
                        }
                    },
                },
            )
        ]
    ) == ["widget/service.yaml.j2: metallb.universe.tf/loadBalancerIPs"]


def test_metallb_version_still_supports_the_metallb_io_annotations():
    """metallb.io/ Service annotations are only read from v0.15 onward.

    A downgrade past that would make every pinned address silently fall back to the auto-assign
    pool, so tie the assertion above to the version pin rather than leaving the two to drift apart.
    """
    pin = K3S_DEFAULTS["k3s_metallb_version"].lstrip("v")
    major, minor = (int(p) for p in pin.split(".")[:2])
    assert (major, minor) >= (0, 15), (
        f"k3s_metallb_version is {pin}, which predates the metallb.io/ Service annotations "
        "the templates now use. Either raise the pin or revert them to metallb.universe.tf/."
    )
