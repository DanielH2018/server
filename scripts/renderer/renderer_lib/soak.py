"""The shared kind `soak`: one Job per revision that judges the services the revision changed.

`bundle.bundle_files` renders it into every bundle (D5.3b2, `SHARED_KINDS`). The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *The soak as a Job (D5.3)*. The bundle gets:

- `flux/soak.yaml`, the Kustomization `soak`. It carries no `dependsOn` edges (D5 Open
  question 19): the Job waits at run time for each Kustomization in its scope to read Ready at
  R. A suspended tag's Kustomizations are listed in `homelab/suspended-deps`, through
  `ordering.drop_suspended`, because they are outside the soak's scope (D5.1b). With no
  edges the Job starts beside the tags, so its wait for them to read Ready at R gets
  `chained_timeout.promote_timeout` over the bundle's other Kustomizations, every one
  counted as changed. Its `timeout` adds the fleet W and two margins, so the Job's wait never
  outlives it;
- `cluster/soak/`: the Job `soak-<spec digest8>` in `flux-system`, which runs
  `ansible/roles/k8s/manifests/files/flux_soak.py` with R's SHA in its arguments; the ConfigMap
  `soak-<content digest8>` holding that script, byte copies of `flux_job_connect.py` and
  `scripts/deploy_tools/changed_set.py`, and `services.json`, the per-service map W reads; and
  the ServiceAccount `soak` with its RBAC.

`services.json` maps each published tag to the longest `for:` among the Prometheus rules that
can page for it, critical or `verify="true"` and not `shadow`, and to the namespaces its tag
renders into. A rule whose `service` label is absent or templated counts for every tag, since
any service's series can carry it.

The Job's spec embeds R's SHA, so its name moves on every commit. `digests.json` therefore
records `soak` over a render at `NORMALISED_REVISION` (C1), and a commit that moves nothing
else leaves `soak` out of C2's changed set. This module imports neither Ansible nor Jinja,
like `bundle.py`, which imports it.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
# `chained_timeout` imports `changed_set` bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[2] / "deploy_tools"))

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.git import git_stdout
from lib.repo_paths import ALL_VARS, K8S_ROLES, REPO, SCRIPTS
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    FLUX_DIR,
    FLUX_INDEX,
    FLUX_NAMESPACE,
    KUSTOMIZATION,
    kustomization_text,
    tag_kustomization,
)
from renderer.renderer_lib.digest8 import configmap_digest8, job_spec_digest8
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    NO_EDGES,
    Edges,
    drop_suspended,
    split_kind,
)

SOAK = "soak"
FILES = K8S_ROLES / "manifests" / "files"
SCRIPT = FILES / "flux_soak.py"
HELPER = FILES / "flux_job_connect.py"
CHANGED_SET = SCRIPTS / "deploy_tools" / "changed_set.py"
SERVICES = "services.json"
RULES = K8S_ROLES / "observability" / "files" / "rules"
FLUX_DEFAULTS = K8S_ROLES / "flux" / "defaults" / "main.yml"
MOUNT = "/opt/soak"
SA_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
# The SHA `digests.json` records `soak` at, so R's own SHA never moves its footprint.
NORMALISED_REVISION = "0" * 40
# kube-state-metrics' `scrape_interval` in observability's prometheus.yaml.j2, which the Job
# uses to bound a scrape gap. test_renderer_soak.py fails a drift.
SCRAPE_SECONDS = 60
# `flux_soak.SCRAPE_AND_EVAL_SECONDS`: the scrape and the rule evaluation W adds to the longest
# `for:`. test_renderer_soak.py fails a drift.
SCRAPE_AND_EVAL_SECONDS = 120
# How far below the `timeout` the Job's `activeDeadlineSeconds` sits, and how far below the
# deadline less W its Kustomization wait ends, so each fails with its own message first.
DEADLINE_MARGIN_S = 60
_SHA = re.compile(r"[0-9a-f]{40}")
_DURATION = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def resolve_revision(given: str | None, repo: Path = REPO) -> str:
    """R's SHA: `given`, or the checkout's `HEAD`, which the publish job pins to R.

    Raises:
        ValueError: `given` is not 40 hex characters, or git fails or prints anything else.
    """
    try:
        # Set only by cross_plane_order.snapshot(): a snapshot is an extracted tree with no
        # version-control metadata, and it may be rendered by an older renderer that has no
        # --revision flag to pass. The publish path sets neither, so it stays on HEAD.
        found = (
            given
            or os.getenv("HOMELAB_RENDER_REVISION")
            or git_stdout("rev-parse", "HEAD", cwd=repo)
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"git rev-parse HEAD failed: {exc}") from exc
    if not _SHA.fullmatch(found):
        raise ValueError(f"--revision takes a 40-hex commit SHA, not {found!r}")
    return found


def prometheus_seconds(duration: str) -> int:
    """A Prometheus duration such as `15m` or `1h30m`, in seconds.

    Raises:
        ValueError: any other form, milliseconds included.
    """
    text = str(duration)
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"the rule duration {duration!r} is not one the soak reads")
    return sum(int(n) * _UNITS[u] for n, u in parts)


def longest_for(rules: Path = RULES) -> dict[str | None, int]:
    """The longest `for:` among the rules the soak reads, by their `service` label.

    A rule counts when it is an alert, `severity: critical` or `verify: "true"`, not
    `shadow: "true"`, and not the Watchdog: the filter `flux_soak.ALERT_FILTER` reads ALERTS
    through. A rule with no `for:` counts 0. The key None collects every rule whose `service`
    label is absent or templated, which can fire for any service.

    Raises:
        ValueError: a rule file is not YAML or a `for:` is not a duration.
    """
    found: dict[str | None, int] = {}
    for path in sorted(rules.glob("*.yaml")) if rules.is_dir() else ():
        try:
            groups = (yaml_fast.safe_load(path.read_text()) or {}).get("groups") or []
        except yaml.YAMLError as exc:
            raise ValueError(f"{path.name} is not YAML: {exc}") from exc
        for group in groups:
            for rule in group.get("rules") or []:
                labels = rule.get("labels") or {}
                if (
                    "alert" not in rule
                    or rule["alert"] == "Watchdog"
                    or str(labels.get("shadow")) == "true"
                    or not (
                        labels.get("severity") == "critical"
                        or str(labels.get("verify")) == "true"
                    )
                ):
                    continue
                service = labels.get("service")
                key = None if service is None or "{{" in str(service) else str(service)
                wait = prometheus_seconds(rule["for"]) if "for" in rule else 0
                found[key] = max(found.get(key, 0), wait)
    return found


def _namespaces(files: Mapping[str, str]) -> set[str]:
    """Every namespace a directory's documents sit in, and every Namespace it creates."""
    found = set()
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata") or {}
            if doc.get("kind") == "Namespace" and meta.get("name"):
                found.add(str(meta["name"]))
            elif meta.get("namespace"):
                found.add(str(meta["namespace"]))
    return found


def service_map(
    tags: Iterable[str],
    directories: Mapping[str, Mapping[str, str]],
    longest: Mapping[str | None, int],
) -> dict[str, dict]:
    """`services.json`: each tag's longest `for:` in seconds, and the namespaces it renders into.

    Args:
        tags: The published tags; the soak's services are tag names, as the `service` label
            of `homelab:pod_service:info` is.
        directories: Each rendered directory's name under `cluster/` mapped to its files.
            `<tag>` and `<tag>-deferred` hold the tag's workloads.
        longest: `longest_for`.
    """
    anywhere = longest.get(None, 0)
    return {
        tag: {
            "for": max(longest.get(tag, 0), anywhere),
            "namespaces": sorted(
                _namespaces(directories.get(tag, {}))
                | _namespaces(directories.get(tag + DEFERRED_SUFFIX, {}))
            ),
        }
        for tag in sorted(set(tags))
    }


def fleet_wait(services: Mapping[str, Mapping], stabilise: int) -> int:
    """W for a scope of every service in `services`, as `flux_soak.wait_seconds` computes it."""
    longest = max((entry["for"] for entry in services.values()), default=0)
    return max(stabilise, longest) + SCRAPE_AND_EVAL_SECONDS


def _binding(name: str, namespace: str) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": name,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": name, "namespace": FLUX_NAMESPACE}
        ],
    }


def rbac(namespaces: Iterable[str]) -> list[dict]:
    """The ServiceAccount `soak` and the reads `flux_soak.py` makes, no more.

    In `flux-system` it reads the OCIRepositories `homelab` and `last-good` and every
    Kustomization. In each namespace a published tag renders into it lists pods and reads
    ReplicaSets, the hop from a Deployment's pod to its Deployment.
    """
    rules: dict[str, list[dict]] = {
        FLUX_NAMESPACE: [
            {
                "apiGroups": ["source.toolkit.fluxcd.io"],
                "resources": ["ocirepositories"],
                "verbs": ["get", "list"],
            },
            {
                "apiGroups": ["kustomize.toolkit.fluxcd.io"],
                "resources": ["kustomizations"],
                "verbs": ["get", "list"],
            },
        ]
    }
    for namespace in namespaces:
        rules.setdefault(namespace, []).extend(
            [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                {
                    "apiGroups": ["apps"],
                    "resources": ["replicasets"],
                    "verbs": ["get", "list"],
                },
            ]
        )
    docs: list[dict] = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": SOAK, "namespace": FLUX_NAMESPACE},
        }
    ]
    for namespace, held in sorted(rules.items()):
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": SOAK, "namespace": namespace},
                "rules": held,
            }
        )
        docs.append(_binding(SOAK, namespace))
    return docs


def _settings() -> dict:
    """The group variables the Job reads: the rollout floor of W and Prometheus's namespace."""
    found = yaml_fast.safe_load(ALL_VARS.read_text()) or {}
    return {
        "stabilise": int(found["k8s_rollout_stabilise_seconds"]),
        "observability": str(found["k8s_observability_namespace"]),
    }


def soak_files(
    revision: str,
    tags: Iterable[str],
    directories: Mapping[str, Mapping[str, str]],
    suspended: Iterable[str] = (),
    rules: Path = RULES,
    kustomizations: Iterable[dict] = (),
) -> dict[str, str]:
    """The `soak` kind's files by bundle path: `flux/soak.yaml` and `cluster/soak/`.

    Args:
        revision: R's commit SHA, which the Job passes the script, or `NORMALISED_REVISION`.
        tags: The published tags.
        directories: Each rendered directory's name under `cluster/` mapped to its files.
        suspended: The tags whose role sets `flux_suspend: true`. Each of their
            Kustomizations is listed in `homelab/suspended-deps`.
        rules: The Prometheus rule files `longest_for` reads; a test passes a fixture.
        kustomizations: Every other Kustomization in the bundle. The Job's wait for the
            changed ones to read Ready at R is their chained timeout with all of them
            changed, because the render cannot know which of them R changes (D5, *Its
            timeout*, under Open question 19).

    Raises:
        ValueError: a rule's `for:` is not a duration.
    """
    tags = sorted(set(tags))
    settings = _settings()
    services = service_map(tags, directories, longest_for(rules))
    wait = fleet_wait(services, settings["stabilise"])
    # Function-level: a host render imports this module, and must not open deploy_tools.
    from deploy_tools.chained_timeout import promote_timeout

    ready = promote_timeout(list(kustomizations))
    deadline = ready + wait + DEADLINE_MARGIN_S
    timeout = deadline + DEADLINE_MARGIN_S
    owned = frozenset(name for name in directories if split_kind(name)[0] in tags)
    # Every Kustomization the soak would wait on, routed through the one suspend step so a
    # suspended tag's names land in the annotation; `kept` is dropped, since the soak waits
    # at run time rather than through `dependsOn`.
    edges = drop_suspended({SOAK: Edges(owned | frozenset(tags))}, suspended)[SOAK]
    kustomization = tag_kustomization(
        SOAK,
        False,
        f"{timeout}s",
        edges=Edges(suspended=edges.suspended) if edges.suspended else NO_EDGES,
    )
    configmap: dict = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "", "namespace": FLUX_NAMESPACE},
        "data": {
            SCRIPT.name: SCRIPT.read_text(),
            HELPER.name: HELPER.read_text(),
            CHANGED_SET.name: CHANGED_SET.read_text(),
            SERVICES: json.dumps(services, sort_keys=True, indent=1) + "\n",
        },
    }
    configmap["metadata"]["name"] = f"{SOAK}-{configmap_digest8(configmap)}"
    prometheus = f"http://prometheus.{settings['observability']}.svc.cluster.local:9090"
    args = {
        "--revision": revision,
        "--services": f"{MOUNT}/{SERVICES}",
        "--stabilise-seconds": str(settings["stabilise"]),
        "--scrape-seconds": str(SCRAPE_SECONDS),
        "--wait-seconds": str(ready),
        "--prometheus": prometheus,
    }
    job: dict = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "",
            "namespace": FLUX_NAMESPACE,
            "annotations": {"homelab/job-kind": SOAK},
        },
        "spec": {
            # A retry would judge R twice, and the second pod would read the first's
            # restarts as its own.
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "template": {
                # Prometheus's fence admits `app: soak` from flux-system (D5.3a). The label
                # must sit on the pod template, which is what a NetworkPolicy reads.
                "metadata": {"labels": {"app": SOAK}},
                "spec": {
                    "serviceAccountName": SOAK,
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "runAsGroup": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            # D5.3b3's annotation query selects `container="soak"`.
                            "name": SOAK,
                            "image": str(
                                yaml_fast.safe_load(FLUX_DEFAULTS.read_text())[
                                    "flux_job_image"
                                ]
                            ),
                            "command": ["python3", f"{MOUNT}/{SCRIPT.name}"],
                            "args": [part for pair in args.items() for part in pair],
                            "env": [
                                {"name": "SSL_CERT_FILE", "value": SA_CA},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                            ],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "script", "mountPath": MOUNT, "readOnly": True}
                            ],
                        }
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
    job["metadata"]["name"] = f"{SOAK}-{job_spec_digest8(job)}"
    namespaces = sorted(
        {ns for entry in services.values() for ns in entry["namespaces"]}
    )
    cluster = {
        "configmap.yaml": yaml.safe_dump(configmap, sort_keys=False),
        "job.yaml": yaml.safe_dump(job, sort_keys=False),
        "rbac.yaml": yaml.safe_dump_all(rbac(namespaces), sort_keys=False),
    }
    cluster[KUSTOMIZATION] = kustomization_text(cluster)
    files = {f"{CLUSTER_DIR}/{SOAK}/{name}": text for name, text in cluster.items()}
    files[f"{FLUX_DIR}/{SOAK}.yaml"] = yaml.safe_dump(kustomization, sort_keys=False)
    return files


def soak_pair(
    revision: str | None,
    tags: Iterable[str],
    directories: Mapping[str, Mapping[str, str]],
    suspended: Iterable[str],
    flux: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """The soak's files at `NORMALISED_REVISION`, for `digests.json`, and as published.

    Args:
        revision: R's SHA, or None to publish the normalised render.
        tags: The published tags.
        directories: Each rendered directory's name under `cluster/` mapped to its files.
        suspended: The tags whose role sets `flux_suspend: true`.
        flux: The bundle's other `flux/` files, `flux/kustomization.yaml` included. The
            published files carry that index again, now listing `soak.yaml`.
    """
    others = [
        doc
        for path, text in sorted(flux.items())
        if path != FLUX_INDEX
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict) and doc.get("kind") == "Kustomization"
    ]
    # A `--role` render may publish a tag whose edge names a flux-owned tag it leaves out.
    # Flux applies that one from its own render, so it adds nothing to this bundle's chain.
    held = {doc["metadata"]["name"] for doc in others}
    for doc in others:
        spec = doc["spec"]
        kept = [d for d in spec.get("dependsOn") or [] if d.get("name") in held]
        spec.pop("dependsOn", None)
        if kept:
            spec["dependsOn"] = kept
    normal = soak_files(
        NORMALISED_REVISION, tags, directories, suspended, kustomizations=others
    )
    published = (
        soak_files(revision, tags, directories, suspended, kustomizations=others)
        if revision
        else normal
    )
    listed = {p for p in {**flux, **published} if p.startswith(f"{FLUX_DIR}/")}
    index = kustomization_text(p.split("/", 1)[1] for p in listed - {FLUX_INDEX})
    return normal, published | {FLUX_INDEX: index}
