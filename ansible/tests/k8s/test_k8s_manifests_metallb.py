"""MetalLB: the two address pools, and the annotation namespace the version still honours.

The ingress VIP must be a single address that is never auto-assigned, the general pool must
not contain it, and the narrowing must land before the ingress pool is created -- a moved
VIP fails silently, because every manifest stays valid. Service annotations must use the
`metallb.io` namespace, and the pinned MetalLB version must still be one that reads it.
"""

import yaml
from _helpers import ANSIBLE

from _manifest_guards import ALL_VARS, K3S, K3S_DEFAULTS, K8S, _render


def _ip_to_int(addr: str) -> int:
    parts = [int(p) for p in addr.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _pool_docs() -> list[dict]:
    """IPAddressPool documents in FILE order — the order kubectl applies them in."""
    rendered = _render(
        K3S / "templates" / "metallb-pool.yaml.j2",
        k3s_metallb_ingress_vip=ALL_VARS["k3s_metallb_ingress_vip"],
        k3s_metallb_pool=yaml.safe_load((K3S / "defaults" / "main.yml").read_text())[
            "k3s_metallb_pool"
        ],
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    return [d for d in docs if d["kind"] == "IPAddressPool"]


def _pools() -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in _pool_docs()}


def test_ingress_pool_is_a_single_address_that_is_never_auto_assigned():
    """`autoAssign: false` is the whole reservation of the ingress address.

    Without it MetalLB hands the ingress address to whichever LoadBalancer Service asks first, and
    ingress moves — after that address is in DNS and, from slice 6, in the router's port-forward.
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
    overlapping pools. Applying ingress-pool ahead of it failed on daniel-box (2026-08-02)
    with:

        CIDR "10.0.0.240/32" in pool "ingress-pool" overlaps with already
        defined CIDR "10.0.0.240/29"

    — homelab-pool still covered .240-.250 at validation time. Reordering the file is the whole
    fix, which is exactly why it is worth a guard: nothing about the YAML looks order-sensitive.
    """
    names = [d["metadata"]["name"] for d in _pool_docs()]
    assert names.index("homelab-pool") < names.index("ingress-pool")


def _metallb_annotation_lines():
    """(template, lineno, line) for every metallb Service annotation in the k8s roles.

    Globs service*.yaml.j2, not service.yaml.j2 — jellyfin pins its LAN address in
    service-lan.yaml.j2, which the narrower glob never saw.
    """
    for tpl in sorted(K8S.glob("*/templates/service*.yaml.j2")):
        for i, line in enumerate(tpl.read_text().splitlines(), 1):
            body = line.split("#", 1)[0]
            if "metallb" in body:
                yield tpl, i, line


def test_metallb_service_annotations_use_the_metallb_io_namespace():
    """Service annotations moved to metallb.io/ in MetalLB v0.15; universe.tf/ is deprecated.

    This assertion is inverted from what it said until 2026-08-28, when the cluster still ran
    v0.14.8 and metallb.io/ genuinely was ignored. v0.16.0 reads both prefixes
    (controller/service.go valueForAnnotation, metallb.io winning) but emits a
    `deprecatedAnnotation` Warning Event per Service on every reconcile for the old one.

    The original hazard is unchanged and is why this guard exists at all: Kubernetes accepts
    any annotation key and MetalLB ignores unrecognised ones, so a wrong prefix is completely
    silent — the Service is created, an address is assigned from the auto-assign pool instead
    of the pinned one, and the deploy is green. Traefik ran on 10.0.0.241 instead of
    10.0.0.240 through an entire slice-1 bring-up because of this.
    """
    for tpl, i, line in _metallb_annotation_lines():
        if "metallb.universe.tf/" in line.split("#", 1)[0]:
            raise AssertionError(
                f"{tpl.relative_to(ANSIBLE)}:{i} uses a deprecated metallb.universe.tf/ "
                f"Service annotation — use metallb.io/. Line: {line.strip()}"
            )


def test_the_metallb_annotation_guard_can_go_red():
    """The rejecting half.

    A guard that matches nothing is indistinguishable from a passing one, and this file has already
    held this assertion pointing the wrong way for six days.
    """
    accepted = "    metallb.io/loadBalancerIPs: 10.0.0.240"
    rejected = "    metallb.universe.tf/loadBalancerIPs: 10.0.0.240"
    assert "metallb.universe.tf/" not in accepted.split("#", 1)[0]
    assert "metallb.universe.tf/" in rejected.split("#", 1)[0]
    # And the glob still finds the real templates, so the loop above is not scanning nothing.
    assert list(_metallb_annotation_lines()), (
        "no metallb Service annotations found — the glob or the templates moved"
    )


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
