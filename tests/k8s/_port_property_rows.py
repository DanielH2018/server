"""Rendered-manifest property rows about a container's declared ports.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re

from _k8s_render import pod_spec
from _property_table import Property
from _workload_property_rows import POD_KINDS

# Kubernetes' IANA_SVC_NAME (k8s.io/apimachinery validation.IsValidPortName): at most 15
# characters, lowercase alphanumerics and '-', alphanumeric at both ends, no "--", one letter.
_PORT_NAME_MAX = 15
_PORT_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_LETTER = re.compile(r"[a-z]")


def _named_ports(role: str, tpl: str, doc: dict):
    """One subject per named container port, init containers included."""
    if doc.get("kind") not in POD_KINDS:
        return
    pod = pod_spec(doc)
    for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
        for port in container.get("ports") or []:
            if "name" in port:
                key = f"{doc['metadata']['name']}:{container['name']}:{port['name']}"
                yield key, port


def _port_name_offence(role: str, port: dict) -> str | None:
    name = str(port["name"])
    if len(name) > _PORT_NAME_MAX:
        return f"port name {name!r} is {len(name)} characters; Kubernetes allows {_PORT_NAME_MAX}"
    if not _PORT_NAME.fullmatch(name):
        return (
            f"port name {name!r} is not lowercase alphanumerics and '-', "
            "starting and ending with an alphanumeric"
        )
    if "--" in name:
        return f"port name {name!r} holds consecutive hyphens"
    if not _LETTER.search(name):
        return f"port name {name!r} holds no letter, so Kubernetes reads it as a number"
    return None


PORT_PROPERTIES = (
    Property(
        name="container-port-names-are-iana-svc-names",
        reason=(
            "Kubernetes validates a container port name as an IANA_SVC_NAME: at most 15 "
            "characters, matching [a-z0-9]([a-z0-9-]*[a-z0-9])? with no '--', and holding a letter. "
            "scripts/validate/k8s_manifests.py's OpenAPI validation does not check it, so "
            "#6537's 16-character `dockerhub-mirror` passed every PR check and first failed "
            "at Flux's dry run, which held the deploy (#6543)."
        ),
        select=_named_ports,
        offence=_port_name_offence,
        red=("registry", {"name": "dockerhub-mirror", "containerPort": 5000}),
        green=("registry", {"name": "dockerhub", "containerPort": 5000}),
        more_red=(
            ("registry", {"name": "Dockerhub", "containerPort": 5000}),
            ("registry", {"name": "docker_hub", "containerPort": 5000}),
            ("registry", {"name": "-dockerhub", "containerPort": 5000}),
            ("registry", {"name": "dockerhub-", "containerPort": 5000}),
            ("registry", {"name": "docker--hub", "containerPort": 5000}),
            ("registry", {"name": "8080", "containerPort": 5000}),
        ),
        more_green=(("registry", {"name": "dockerhub-mirro", "containerPort": 5000}),),
        # 56 named ports render. Close enough to notice a contraction.
        min_matches=50,
        must_find=frozenset({"registry:registry:registry", "traefik:traefik:http"}),
    ),
)
