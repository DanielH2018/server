"""Rendered-manifest property rows about how the kubelet sets up a pod's volumes.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

from types import MappingProxyType

from _k8s_render import pod_spec
from _property_table import Property
from _workload_property_rows import POD_KINDS

_FLUX_CONTROLLERS = (
    "source-controller",
    "kustomize-controller",
    "notification-controller",
)


def _fs_group_pods(role: str, tpl: str, doc: dict):
    if doc.get("kind") not in POD_KINDS:
        return
    context = pod_spec(doc).get("securityContext") or {}
    if "fsGroup" in context:
        yield f"{doc['kind']}/{doc['metadata']['name']}", context


def _fs_group_walk_offence(role: str, context: dict) -> str | None:
    if context.get("fsGroupChangePolicy") == "OnRootMismatch":
        return None
    return (
        f"sets fsGroup with fsGroupChangePolicy {context.get('fsGroupChangePolicy')!r}, so the "
        "kubelet re-chowns every file on the volume at each mount"
    )


VOLUME_PROPERTIES = (
    Property(
        name="pods-with-fsgroup-skip-the-ownership-walk",
        reason=(
            "With fsGroup and no fsGroupChangePolicy, the kubelet walks a volume and resets "
            "group ownership on every file each time the pod mounts it. janitorr mounts the "
            "shared `media-data` library read-write, so its restart cost grows with the library "
            "(#8220). `OnRootMismatch` skips the walk once the volume root carries the group, "
            "and still chowns a fresh root-owned volume such as navidrome's Longhorn claim. "
            "`pod_shell` emits it beside `fs_group`; a pod that writes its securityContext by "
            "hand sets it there."
        ),
        select=_fs_group_pods,
        offence=_fs_group_walk_offence,
        red=("janitorr", {"fsGroup": 1000}),
        green=("janitorr", {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"}),
        more_red=(("janitorr", {"fsGroup": 1000, "fsGroupChangePolicy": "Always"}),),
        must_find=frozenset({"Deployment/janitorr", "Deployment/navidrome"}),
        allow=MappingProxyType(
            {
                f"Deployment/{name}": (
                    "vendored Flux install (files/gotk-components.yaml); its only volumes are "
                    "emptyDirs, which start empty in every pod, so there is no tree to walk"
                )
                for name in _FLUX_CONTROLLERS
            }
        ),
    ),
)
