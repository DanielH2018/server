"""Rendered-manifest property rows over a pod's kernel confinement.

`test_rendered_properties.py` runs these rows beside its own; the rules for what makes a row are
in its docstring.
"""

from _property_table import Property
from _workload_property_rows import (
    MIN_POD_TEMPLATES,
    POD_TEMPLATES_MUST_FIND,
    pod_specs,
)


def _unconfined_seccomp_offence(role: str, pod: dict) -> str | None:
    levels = [("the pod", pod.get("securityContext") or {})]
    for key in ("initContainers", "containers", "ephemeralContainers"):
        for container in pod.get(key) or []:
            levels.append(
                (
                    f"container {container.get('name')!r}",
                    container.get("securityContext") or {},
                )
            )
    unconfined = [
        where
        for where, context in levels
        if (context.get("seccompProfile") or {}).get("type") == "Unconfined"
    ]
    if not unconfined:
        return None
    return (
        f"{', '.join(unconfined)} sets seccompProfile Unconfined, which opts out of the "
        "kubelet's seccomp-default. Drop it, or add an `allow` entry giving the reason"
    )


def _seccomp(profile_type: str) -> dict:
    return {"seccompProfile": {"type": profile_type}}


SECURITY_PROPERTIES = (
    Property(
        name="no-pod-runs-seccomp-unconfined",
        reason=(
            "Each k3s node's kubelet runs with `seccomp-default=true` (CIS 1.12 check 4.2.14), "
            "so a container whose pod names no profile gets the container runtime's default "
            "seccomp filter. An explicit `seccompProfile: {type: Unconfined}`, at pod or "
            "container level, is the one way back out, and it lifts the filter for that "
            "container without any other sign in the manifest. Each one therefore needs an "
            "`allow` entry that gives its reason. The list starts empty: image-builder's build "
            "Job is the one Unconfined user, and it renders outside this corpus "
            "(validate.render_lib.k8s.SKIP_ROLES), so test_image_builder_security_context.py "
            "owns it."
        ),
        select=pod_specs,
        offence=_unconfined_seccomp_offence,
        red=("nut", {"securityContext": _seccomp("Unconfined")}),
        green=("nut", {"containers": [{"name": "nut"}]}),
        more_red=(
            (
                "nut",
                {
                    "containers": [
                        {"name": "nut", "securityContext": _seccomp("Unconfined")}
                    ]
                },
            ),
            (
                "nut",
                {
                    "initContainers": [
                        {"name": "init", "securityContext": _seccomp("Unconfined")}
                    ],
                    "containers": [{"name": "nut"}],
                },
            ),
        ),
        more_green=(
            (
                "nut",
                {
                    "securityContext": _seccomp("RuntimeDefault"),
                    "containers": [
                        {"name": "nut", "securityContext": _seccomp("Localhost")}
                    ],
                },
            ),
        ),
        min_matches=MIN_POD_TEMPLATES,
        must_find=POD_TEMPLATES_MUST_FIND,
    ),
)
