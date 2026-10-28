"""The `<tag>-prepull` directory: Jobs that pull a role's images onto its node before `<tag>` applies.

A `Recreate` Deployment stops its old pod before the new pod's image pull starts, so a role
whose image is large and whose registry is slow is down for the whole pull. During a slow-GHCR
episode, code-server pulled 851 MB at about 0.28 MB/s, about 50 minutes (#5525); GHCR's normal
rate is 44-98 MB/s (#5654). A role that sets
`flux_prepull_timeout` in its `defaults/main.yml` gets `DIR/cluster/<tag>-prepull/`, which
holds:

- one Job `prepull-<tag>-<spec digest8>` in `flux-system` per distinct placement among the
  tag's workloads. A placement is the pod's `nodeSelector`, node affinity and tolerations,
  copied onto the Job, so the scheduler puts the Job where it would put the workload: on the
  preferred node, or on another one when that node is cordoned or full. Each Job runs one
  container per distinct image those workloads run, init containers included, with
  `imagePullPolicy: IfNotPresent`. A container starts only once the kubelet has its image, so
  the Job completes exactly when every image is on the node;
- the ConfigMap `prepull-<tag>-<content digest8>` holding the shell script each container
  runs, which exits at once. The Job census requires every Job container to run a mounted
  script, so even a no-op is delivered that way.

`ordering.py` gives `<tag>-prepull` the edges of `<tag>` and makes `<tag>` depend on it, so the
old pod keeps serving through the pull. The Kustomization's `timeout` is the role's
`flux_prepull_timeout` (`bundle.flux_files`). The wait is moved, not removed: the pull still
holds a kustomize-controller worker and still sits on `verify`'s chained timeout.

DECIDED: the Job sets no `activeDeadlineSeconds`. A slow pull never fails a Pod: it waits in
`ContainerCreating` or `ImagePullBackOff`. So a deadline is the only way the Job could fail,
and Flux never re-runs a failed Job under the same name. Every later revision that kept the
image would then hold `<tag>` at `DependencyNotReady` until a new content tag renamed the
Job. Without a deadline, a pull that outlasts the Kustomization's `timeout` costs that
revision its verdict, and the Job keeps pulling. Flux's next retry finds it complete and
applies `<tag>`.

Each Job's spec carries the image refs and the placement, so a content-tag change renames the
Job and Flux runs it again, while a revision that leaves the images alone keeps the completed
Job. The scheduler places the Job and the workload's new pod separately, so a node that is
cordoned between the two still pays a cold pull. Every image must carry `sh`, which runs the
script.

A role that also sets `flux_prepull_workloads` pulls for the Deployments and StatefulSets it
names and no others (`ordering.prepull_workloads`, #5875). Only those must name a node and
carry `sh`. observability names Grafana alone: its 482 MB image is the one large pull, and
loki, tempo and kube-state-metrics pin no node and ship no shell.

DECIDED: the no-op is a shell script, not Python. The kubelet runs each pulled image, so the
interpreter must be in every image a workload names. n8n's image and the runners' `alpine`
init container carry no `python3` (#5546), and a container that cannot start fails the Job
the way a deadline would. Every image the pre-pull Jobs pull carries `sh`. The script runs
no network call, so the Job census holds it byte-identical to `SCRIPT` instead of reading it
the way it reads a Python script.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

import json

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
from renderer.renderer_lib.ordering import prepull_timeouts, prepull_workloads

HOSTNAME = "kubernetes.io/hostname"
WORKLOADS = frozenset({"Deployment", "StatefulSet"})
MOUNT = "/opt/prepull"
SCRIPT_NAME = "prepull.sh"
SCRIPT = (
    "# Exits at once: this container started, so the kubelet holds its image (#5525).\n"
    'echo "image present on this node"\n'
)
COMMAND = ["sh", f"{MOUNT}/{SCRIPT_NAME}"]


def _pod_specs(files: Mapping[str, str]) -> list[tuple[str, dict]]:
    specs = []
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") in WORKLOADS:
                name = str((doc.get("metadata") or {}).get("name", ""))
                template = (doc.get("spec") or {}).get("template") or {}
                specs.append((name, template.get("spec") or {}))
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


def placement(spec: Mapping) -> dict:
    """The fields of a pod spec that decide its node: `nodeSelector`, node affinity, tolerations.

    Pod affinity is left out, because a Job that copied a workload's anti-affinity to its own
    pods would repel itself from the node it should pull onto.
    """
    found: dict = {}
    if spec.get("nodeSelector"):
        found["nodeSelector"] = spec["nodeSelector"]
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity")
    if node_affinity:
        found["affinity"] = {"nodeAffinity": node_affinity}
    if spec.get("tolerations"):
        found["tolerations"] = spec["tolerations"]
    return found


def targets(
    tag: str, files: Mapping[str, str], workloads: Iterable[str] | None = None
) -> list[tuple[dict, list[str]]]:
    """Each distinct placement among `tag`'s rendered workloads, with the images run there.

    Args:
        tag: The deploy tag.
        files: The tag's rendered files, by name.
        workloads: The workload names to pull for (`flux_prepull_workloads`), or None for
            every Deployment and StatefulSet in the render.

    Raises:
        RenderError: The render holds no Deployment or StatefulSet, holds none of a name in
            `workloads`, or holds one to pull for that names no node.
    """
    named = _pod_specs(files)
    if not named:
        raise RenderError(
            f"{tag} sets flux_prepull_timeout, and its render holds no Deployment or "
            "StatefulSet whose images a pre-pull could fetch"
        )
    if workloads is not None:
        wanted = list(workloads)
        missing = sorted(set(wanted) - {name for name, _ in named})
        if missing:
            raise RenderError(
                f"{tag}'s flux_prepull_workloads names {', '.join(missing)}, which its render "
                "holds no Deployment or StatefulSet of"
            )
        named = [(name, spec) for name, spec in named if name in wanted]
    specs = [spec for _, spec in named]
    if not all(nodes(s) for s in specs):
        raise RenderError(
            f"{tag} sets flux_prepull_timeout, and a workload in its render names no node by "
            f"{HOSTNAME}, so the pre-pull cannot tell where the pod will run"
        )
    grouped: dict[str, tuple[dict, list[str]]] = {}
    for spec in specs:
        where = placement(spec)
        held = grouped.setdefault(json.dumps(where, sort_keys=True), (where, []))[1]
        held += [i for i in images(spec) if i not in held]
    return list(grouped.values())


def prepull_files(tag: str, pulls: Iterable[tuple[dict, list[str]]]) -> dict[str, str]:
    """The files of `cluster/<tag>-prepull/`, by name, `kustomization.yaml` included.

    Args:
        tag: The deploy tag.
        pulls: Each placement to pull under, one Job each, with the images that Job pulls,
            one container each (`targets`).
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
    for where, image_refs in pulls:
        job: dict = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "",
                "namespace": FLUX_NAMESPACE,
                "annotations": {"homelab/job-kind": "prepull"},
            },
            "spec": {
                # A slow or failing pull leaves the pod waiting, not failed, so a retry
                # would add nothing. No deadline: the module docstring's DECIDED says why.
                "backoffLimit": 0,
                "template": {
                    "metadata": {"labels": {"app": base}},
                    "spec": {
                        **where,
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
                                "command": list(COMMAND),
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
        RenderError: A tag's timeout or workload names cannot be read, or its render holds
            no workload, lacks a named one, or holds one to pull for that names no node.
    """
    tags = set(published) & {t.tag for t in rendered}
    timeouts, problems = prepull_timeouts(tags, roles)
    scopes, scope_problems = prepull_workloads(tags, roles)
    problems += scope_problems
    if problems:
        raise RenderError("; ".join(problems))
    attached = []
    for tag in rendered:
        if tag.tag not in timeouts:
            attached.append(tag)
            continue
        pulls = targets(tag.tag, tag.files, scopes.get(tag.tag))
        attached.append(replace(tag, prepull=prepull_files(tag.tag, pulls)))
    return attached
