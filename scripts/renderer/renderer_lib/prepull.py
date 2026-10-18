"""The `<tag>-prepull` directory: Jobs that pull a role's images onto its node before `<tag>` applies.

A `Recreate` Deployment stops its old pod before the new pod's image pull starts, so a role
whose image is large and whose registry is slow is down for the whole pull. code-server pulls
851 MB from GHCR at about 0.28 MB/s, about 50 minutes (#5525). A role that sets
`flux_prepull_timeout` in its `defaults/main.yml` gets `DIR/cluster/<tag>-prepull/`, which
holds:

- one Job `prepull-<tag>-<spec digest8>` in `flux-system` per node the tag's workloads name in
  their node selector or node affinity. Each Job is pinned to its node and runs one container
  per distinct image the workloads run, init containers included, with
  `imagePullPolicy: IfNotPresent`. A container starts only once the kubelet has its image, so
  the Job completes exactly when every image is on the node;
- the ConfigMap `prepull-<tag>-<content digest8>` holding the script each container runs, which
  exits at once. The Job census requires every Job container to run a mounted script, so
  even a no-op is delivered that way.

`ordering.py` gives `<tag>-prepull` the edges of `<tag>` and makes `<tag>` depend on it, so the
old pod keeps serving through the pull. The Kustomization's `timeout` is the role's
`flux_prepull_timeout` (`bundle.flux_files`), and each Job's `activeDeadlineSeconds` sits
`DEADLINE_MARGIN_S` below it. The wait is moved, not removed: the pull still holds a
kustomize-controller worker and still sits on `verify`'s chained timeout.

Each Job's spec carries the image refs and the node, so a content-tag change renames the Job
and Flux runs it again, while a revision that leaves the images alone keeps the completed Job.

A workload pinned only by a preference can still land on another node, and there the rollout
pays the cold pull as before. Every image must carry `python3`, which runs the script.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle import (
    FLUX_NAMESPACE,
    KUSTOMIZATION,
    kustomization_text,
)
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.digest8 import configmap_digest8, job_spec_digest8
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import prepull_timeouts
from renderer.renderer_lib.snapshot import DEADLINE_MARGIN_S, seconds

HOSTNAME = "kubernetes.io/hostname"
WORKLOADS = frozenset({"Deployment", "StatefulSet"})
MOUNT = "/opt/prepull"
SCRIPT_NAME = "prepull.py"
SCRIPT = (
    '"""Exits at once: this container started, so the kubelet holds its image (#5525)."""\n'
    "\n"
    'print("image present on this node")\n'
)


def _pod_specs(files: Mapping[str, str]) -> list[dict]:
    specs = []
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") in WORKLOADS:
                template = (doc.get("spec") or {}).get("template") or {}
                specs.append(template.get("spec") or {})
    return specs


def _hostnames(terms: Iterable[Mapping]) -> list[str]:
    return [
        str(value)
        for term in terms
        for expr in term.get("matchExpressions") or []
        if expr.get("key") == HOSTNAME and expr.get("operator") == "In"
        for value in expr.get("values") or []
    ]


def nodes(spec: Mapping) -> list[str]:
    """The node names a pod spec selects or prefers by `kubernetes.io/hostname`, in order.

    It reads `nodeSelector`, then the required node affinity, then the preferred one.
    """
    found = []
    selector = (spec.get("nodeSelector") or {}).get(HOSTNAME)
    if selector:
        found.append(str(selector))
    affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    found += _hostnames(required.get("nodeSelectorTerms") or [])
    preferred = affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []
    found += _hostnames(p.get("preference") or {} for p in preferred)
    return list(dict.fromkeys(found))


def images(spec: Mapping) -> list[str]:
    """Each distinct image a pod spec runs, init containers first, in order."""
    containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
    return list(dict.fromkeys(str(c["image"]) for c in containers if c.get("image")))


def targets(tag: str, files: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """The nodes to pull onto and the images to pull, read from `tag`'s rendered workloads.

    Raises:
        RenderError: The render holds no Deployment or StatefulSet, or none names a node.
    """
    specs = _pod_specs(files)
    if not specs:
        raise RenderError(
            f"{tag} sets flux_prepull_timeout, and its render holds no Deployment or "
            "StatefulSet whose images a pre-pull could fetch"
        )
    found_nodes = list(dict.fromkeys(n for s in specs for n in nodes(s)))
    if not found_nodes:
        raise RenderError(
            f"{tag} sets flux_prepull_timeout, and no workload in its render names a node by "
            f"{HOSTNAME}, so the pre-pull cannot tell where the pod will run"
        )
    return found_nodes, list(dict.fromkeys(i for s in specs for i in images(s)))


def prepull_files(
    tag: str, node_names: Iterable[str], image_refs: list[str], deadline: int
) -> dict[str, str]:
    """The files of `cluster/<tag>-prepull/`, by name, `kustomization.yaml` included.

    Args:
        tag: The deploy tag.
        node_names: The nodes to pull onto, one Job each.
        image_refs: The images each Job pulls, one container each.
        deadline: Each Job's `activeDeadlineSeconds`, below its Kustomization's `timeout`.
    """
    base = f"prepull-{tag}"
    configmap: dict = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "", "namespace": FLUX_NAMESPACE},
        "data": {SCRIPT_NAME: SCRIPT},
    }
    configmap["metadata"]["name"] = f"{base}-{configmap_digest8(configmap)}"
    jobs = []
    for node in node_names:
        job: dict = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "",
                "namespace": FLUX_NAMESPACE,
                "annotations": {"homelab/job-kind": "prepull"},
            },
            "spec": {
                # An image that cannot be pulled leaves the pod waiting, not failed, so only
                # the deadline ends it and a retry would add nothing.
                "backoffLimit": 0,
                "activeDeadlineSeconds": deadline,
                "template": {
                    "metadata": {"labels": {"app": base}},
                    "spec": {
                        "nodeSelector": {HOSTNAME: node},
                        "restartPolicy": "Never",
                        "enableServiceLinks": False,
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65534,
                            "runAsGroup": 65534,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": f"pull-{index}",
                                "image": ref,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["python3", f"{MOUNT}/{SCRIPT_NAME}"],
                                "env": [
                                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}
                                ],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "16Mi"},
                                    "limits": {"memory": "64Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "script",
                                        "mountPath": MOUNT,
                                        "readOnly": True,
                                    }
                                ],
                            }
                            for index, ref in enumerate(image_refs)
                        ],
                        "volumes": [
                            {
                                "name": "script",
                                "configMap": {"name": configmap["metadata"]["name"]},
                            }
                        ],
                    },
                },
            },
        }
        job["metadata"]["name"] = f"{base}-{job_spec_digest8(job)}"
        jobs.append(job)
    files = {
        "configmap.yaml": yaml.safe_dump(configmap, sort_keys=False),
        "job.yaml": yaml.safe_dump_all(jobs, sort_keys=False),
    }
    files[KUSTOMIZATION] = kustomization_text(files)
    return files


def attach_prepulls(
    rendered: list[RenderedTag], published: Iterable[str], roles: Path = K8S_ROLES
) -> list[RenderedTag]:
    """`rendered` with `<tag>-prepull` added for each published tag that sets the timeout.

    Runs after `images.ghcr_refs`, so each Job pulls the ref the workload will run.

    Args:
        rendered: The rendered tags, as they will be written.
        published: The tags the bundle publishes. No other tag gets a Kustomization.
        roles: The role tree whose defaults set `flux_prepull_timeout`.

    Raises:
        RenderError: A tag's timeout cannot be read or leaves the Job no time, or its render
            names no node or no workload.
    """
    timeouts, problems = prepull_timeouts(
        set(published) & {t.tag for t in rendered}, roles
    )
    if problems:
        raise RenderError("; ".join(problems))
    attached = []
    for tag in rendered:
        timeout = timeouts.get(tag.tag)
        if timeout is None:
            attached.append(tag)
            continue
        deadline = seconds(timeout) - DEADLINE_MARGIN_S
        if deadline <= 0:
            raise RenderError(
                f"{tag.tag}'s flux_prepull_timeout of {timeout} leaves the pre-pull Job no time"
            )
        node_names, image_refs = targets(tag.tag, tag.files)
        attached.append(
            replace(
                tag, prepull=prepull_files(tag.tag, node_names, image_refs, deadline)
            )
        )
    return attached
