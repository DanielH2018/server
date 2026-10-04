"""MetalLB: the two address pools, and the annotation namespace the version still honours.

The ingress VIP must be a single address that is never auto-assigned, the general pool must
not contain it, and the narrowing must land before the ingress pool is created -- a moved
VIP fails silently, because every manifest stays valid. The pinned MetalLB version must still
read the `metallb.io` Service annotations, which the
`metallb-service-annotations-use-metallb-io` row of `_edge_property_rows.py` requires.
"""

from lib import yaml_fast

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


def test_metallb_version_still_supports_the_metallb_io_annotations():
    """metallb.io/ Service annotations are only read from v0.15 onward.

    A downgrade past that would make every pinned address silently fall back to the auto-assign
    pool, so tie the annotation row to the version pin rather than leaving the two to drift apart.
    """
    pin = K3S_DEFAULTS["k3s_metallb_version"].lstrip("v")
    major, minor = (int(p) for p in pin.split(".")[:2])
    assert (major, minor) >= (0, 15), (
        f"k3s_metallb_version is {pin}, which predates the metallb.io/ Service annotations "
        "the templates use. Either raise the pin or revert them to metallb.universe.tf/."
    )
