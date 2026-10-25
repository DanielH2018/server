"""Rendered-manifest property rows about how a container's image is pulled.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

from _k8s_render import pod_spec
from _property_table import Property
from _workload_property_rows import POD_KINDS

_DIGEST = "@sha256:"


def _digest_pinned_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") not in POD_KINDS:
        return
    spec = pod_spec(doc)
    for container in (spec.get("initContainers") or []) + (
        spec.get("containers") or []
    ):
        if _DIGEST in str(container.get("image", "")):
            yield f"{doc['metadata']['name']}:{container['name']}", container


def _pull_if_not_present_offence(role: str, container: dict) -> str | None:
    policy = container.get("imagePullPolicy")
    if policy == "IfNotPresent":
        return None
    return (
        f"imagePullPolicy is {policy!r} on a digest-pinned image, so a restart with the "
        "registry unreachable cannot start the container the node already holds. Add "
        "`imagePullPolicy: IfNotPresent` next to `image:`"
    )


IMAGE_PROPERTIES = (
    Property(
        name="digest-pinned-images-pull-if-not-present",
        reason=(
            "A digest is immutable, so pulling it again buys nothing. Kubernetes defaults the "
            "policy to `Always` for a `:latest` tag whatever digest follows it, and the API "
            "server applies that default only at CREATE time: a Deployment created as "
            "`:latest@sha256:` keeps `Always` after a retag that leaves the field unset. So the "
            "policy is pinned explicitly on every digest ref, not only on `:latest` ones. "
            "On 2026-10-22 the WAN was down 17:01-18:22 UTC and qbittorrent "
            "(`lscr.io/linuxserver/qbittorrent:latest@sha256:`) restarted into 71 minutes of "
            "ImagePullBackOff with the digest already on the node (#6136). Built images "
            "(`localhost:`) carry no digest and keep `Always`, per `built-images-pull-always`."
        ),
        select=_digest_pinned_containers,
        offence=_pull_if_not_present_offence,
        red=(
            "qbittorrent",
            {"image": "lscr.io/linuxserver/qbittorrent:latest@sha256:ab"},
        ),
        green=(
            "qbittorrent",
            {
                "image": "lscr.io/linuxserver/qbittorrent:latest@sha256:ab",
                "imagePullPolicy": "IfNotPresent",
            },
        ),
        more_red=(
            # A versioned tag defaults to IfNotPresent only on create, so it is pinned too.
            ("pihole", {"image": "pihole/pihole:2026.09.0@sha256:ab"}),
            (
                "peanut",
                {
                    "image": "brandawg93/peanut:latest@sha256:ab",
                    "imagePullPolicy": "Always",
                },
            ),
        ),
        # 133 digest-pinned containers render. Close enough to notice a contraction.
        min_matches=120,
        must_find=frozenset(
            {
                "qbittorrent:qbittorrent",
                "peanut:peanut",
                "cloudflare-ddns-direct:cloudflare-ddns",
            }
        ),
    ),
)
