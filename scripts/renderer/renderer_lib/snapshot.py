"""The `<tag>-snapshot` directory: one Job that snapshots a role's claims before `<tag>` applies.

For each published tag whose role declares `k8s_autodeploy_snapshot_pvcs` (`snapshot_claims`),
the render writes `DIR/cluster/<tag>-snapshot/` (target slice D5, *Snapshots as a Job per role
revision (D5.2a)*). It holds:

- the Job `snapshot-<tag>-<spec digest8>` in `flux-system`, which runs
  `ansible/roles/k8s/manifests/files/flux_snapshot.py`. Its spec carries the role's `digest8`,
  the first eight hex characters of `digests.json`'s `own` for `<tag>`, and the claims, so a
  change to the role's content renames the Job and Flux runs it again;
- the ConfigMap `snapshot-<tag>-<content digest8>` with the script and a byte copy of
  `flux_job_connect.py`. The Job's spec names it, so a script fix renames the Job too;
- the ServiceAccount `snapshot-<tag>` and its RBAC: create, list and delete Snapshots and
  read Volumes in `longhorn-system`, read the `<tag>` Kustomization, and, in a ClusterRole,
  read the claims' PVCs by name and every PV, whose names Longhorn generates.

`ordering.py` gives `<tag>-snapshot` the edges of `<tag>` and makes `<tag>` depend on it. The
Job census in `scripts/renderer/tests/_job_census.py` reads the directory like any other.

Each claim's namespace is the namespace of the PersistentVolumeClaim `<tag>` renders under that
name. A snapshot claim the tag renders no PVC for fails the render, as does a snapshot name
that could pass Longhorn's 63-byte delete limit.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.k8s_roles import snapshot_claims
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle import (
    CLUSTER_DIR,
    FLUX_NAMESPACE,
    KUSTOMIZATION,
    configmap_digest8,
    digest,
    job_spec_digest8,
    kustomization_text,
)
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.environment import RenderError

FILES = K8S_ROLES / "manifests" / "files"
SCRIPT = FILES / "flux_snapshot.py"
HELPER = FILES / "flux_job_connect.py"
SNAPSHOT_DEFAULTS = K8S_ROLES / "volume-snapshot" / "defaults" / "main.yml"
FLUX_DEFAULTS = K8S_ROLES / "flux" / "defaults" / "main.yml"
MOUNT = "/opt/snapshot"
# Longhorn's delete webhook refuses a longer snapshot name through every route (#2734).
NAME_LIMIT = 63
# `%Y%m%d%H%M%S`, the token the Job appends at run time.
TOKEN_WIDTH = 14
# How far below its Kustomization's `timeout` the Job's `activeDeadlineSeconds` sits, so the
# Job fails with its own message before Flux reports a bare timeout.
DEADLINE_MARGIN_S = 60
SA_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def seconds(duration: str) -> int:
    """A Go duration of whole hours, minutes and seconds, such as `600s`, in seconds.

    Raises:
        RenderError: Any other form.
    """
    match = _DURATION.fullmatch(duration)
    if not duration or match is None:
        raise RenderError(f"{duration!r} is not a duration of h, m and s")
    hours, minutes, secs = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + secs


def snapshot_name(
    tag: str, digest8: str, claim: str, token: str = "0" * TOKEN_WIDTH
) -> str:
    """The Job's snapshot name for `claim`, with a token of the run-time width by default.

    `<claim>` drops a leading `<tag>-`, by the rule `claim.yml` and `volume-revert` use.
    `flux_snapshot.snapshot_name` builds the same name at run time.
    """
    short = claim.removeprefix(f"{tag}-") or claim
    return f"autodeploy-{tag}-{digest8}-{short}-{token}"


def role_digest8(tag: RenderedTag) -> str:
    """The first eight hex characters of `own`, the digest `digests.json` records for `tag`.

    `own` covers `cluster/<tag>/` alone, so this is computed from the directory as written.
    """
    return digest(
        {f"{CLUSTER_DIR}/{tag.tag}/{f}": t.encode() for f, t in tag.files.items()}
    )[:8]


def claim_namespaces(
    tag: str, files: Mapping[str, str], claims: Iterable[str]
) -> dict[str, str]:
    """Each of `claims` mapped to the namespace of the PVC that `tag`'s render names after it.

    Raises:
        RenderError: A claim matches no rendered PersistentVolumeClaim.
    """
    rendered = {}
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
                meta = doc.get("metadata") or {}
                rendered[str(meta.get("name"))] = str(meta.get("namespace"))
    missing = sorted(set(claims) - set(rendered))
    if missing:
        raise RenderError(
            f"{tag} declares the snapshot claims {missing} in k8s_autodeploy_snapshot_pvcs, "
            "and its render holds no PersistentVolumeClaim of that name"
        )
    return {claim: rendered[claim] for claim in claims}


def _binding(kind: str, name: str, namespace: str | None) -> dict:
    meta = {"name": name} | ({"namespace": namespace} if namespace else {})
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": f"{kind}Binding",
        "metadata": meta,
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": kind,
            "name": name,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": name, "namespace": FLUX_NAMESPACE}
        ],
    }


def rbac(tag: str, claims: Mapping[str, str]) -> list[dict]:
    """The ServiceAccount `snapshot-<tag>` and the grants `flux_snapshot.py` uses, no more.

    The Roles sit only in `longhorn-system` and `flux-system`, which exist before any tag
    applies. The PVC read goes in the ClusterRole, narrowed to the claim names, because a
    Role in a namespace `<tag>` creates would fail the snapshot's apply on the tag's first
    revision, which `<tag>` waits on.
    """
    name = f"snapshot-{tag}"
    rules: dict[str, list[dict]] = {
        "longhorn-system": [
            {
                "apiGroups": ["longhorn.io"],
                "resources": ["snapshots"],
                "verbs": ["get", "list", "create", "delete"],
            },
            {"apiGroups": ["longhorn.io"], "resources": ["volumes"], "verbs": ["get"]},
        ],
        FLUX_NAMESPACE: [
            {
                "apiGroups": ["kustomize.toolkit.fluxcd.io"],
                "resources": ["kustomizations"],
                "resourceNames": [tag],
                "verbs": ["get"],
            }
        ],
    }
    docs: list[dict] = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": FLUX_NAMESPACE},
        }
    ]
    for namespace, held in sorted(rules.items()):
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": name, "namespace": namespace},
                "rules": held,
            }
        )
        docs.append(_binding("Role", name, namespace))
    docs.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": name},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["persistentvolumeclaims"],
                    "resourceNames": sorted(claims),
                    "verbs": ["get"],
                },
                # Longhorn generates a PV's name, so this grant cannot be narrowed.
                {
                    "apiGroups": [""],
                    "resources": ["persistentvolumes"],
                    "verbs": ["get"],
                },
            ],
        }
    )
    docs.append(_binding("ClusterRole", name, None))
    return docs


def snapshot_files(
    tag: str,
    digest8: str,
    claims: Mapping[str, str],
    image: str,
    retain: int,
    timeout: int,
    poll: int,
    deadline: int,
    script: str,
    helper: str,
) -> dict[str, str]:
    """The files of `cluster/<tag>-snapshot/`, by name, `kustomization.yaml` included.

    Args:
        tag: The deploy tag.
        digest8: The role's `digest8` (`role_digest8`).
        claims: Each snapshot claim mapped to its namespace.
        image: `flux_job_image`.
        retain: `volume_snapshot_retain`; the script clamps it to a floor of 2.
        timeout: `volume_snapshot_timeout`, the seconds a snapshot has to become ready.
        poll: `volume_snapshot_poll_interval`, in seconds.
        deadline: The Job's `activeDeadlineSeconds`, below its Kustomization's `timeout`.
        script: The text of `flux_snapshot.py`.
        helper: The text of `flux_job_connect.py`, copied byte for byte.

    Raises:
        RenderError: The longest snapshot name would pass `NAME_LIMIT` bytes.
    """
    longest = max((snapshot_name(tag, digest8, c) for c in claims), key=len)
    if len(longest.encode()) > NAME_LIMIT:
        raise RenderError(
            f"{tag}'s snapshot name {longest} is {len(longest.encode())} bytes, over "
            f"Longhorn's {NAME_LIMIT}-byte delete limit; shorten the tag or the claim name"
        )
    base = f"snapshot-{tag}"
    configmap: dict = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "", "namespace": FLUX_NAMESPACE},
        "data": {SCRIPT.name: script, HELPER.name: helper},
    }
    configmap["metadata"]["name"] = f"{base}-{configmap_digest8(configmap)}"
    env = {
        "SNAPSHOT_TAG": tag,
        "SNAPSHOT_DIGEST8": digest8,
        "SNAPSHOT_CLAIMS": json.dumps(
            [{"name": c, "namespace": claims[c]} for c in sorted(claims)],
            separators=(",", ":"),
        ),
        "SNAPSHOT_RETAIN": str(retain),
        "SNAPSHOT_TIMEOUT": str(timeout),
        "SNAPSHOT_POLL": str(poll),
        "SSL_CERT_FILE": SA_CA,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    job: dict = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "",
            "namespace": FLUX_NAMESPACE,
            "annotations": {"homelab/job-kind": "snapshot"},
        },
        "spec": {
            # One retry, which takes a fresh token, so it never collides with the first try.
            "backoffLimit": 1,
            "activeDeadlineSeconds": deadline,
            "template": {
                "metadata": {"labels": {"app": base}},
                "spec": {
                    "serviceAccountName": base,
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
                            "name": "snapshot",
                            "image": image,
                            "command": ["python3", f"{MOUNT}/{SCRIPT.name}"],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
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
    job["metadata"]["name"] = f"{base}-{job_spec_digest8(job)}"
    files = {
        "configmap.yaml": yaml.safe_dump(configmap, sort_keys=False),
        "job.yaml": yaml.safe_dump(job, sort_keys=False),
        "rbac.yaml": yaml.safe_dump_all(rbac(tag, claims), sort_keys=False),
    }
    files[KUSTOMIZATION] = kustomization_text(files)
    return files


def _defaults(path: Path) -> dict:
    return yaml_fast.safe_load(path.read_text()) or {}


def attach_snapshots(
    rendered: list[RenderedTag],
    published: Iterable[str],
    timeout: str,
    roles: Path = K8S_ROLES,
) -> list[RenderedTag]:
    """`rendered` with `<tag>-snapshot` added for each published tag that declares claims.

    Runs after every pass that edits `cluster/<tag>/`, because the Job embeds that directory's
    digest, which must equal the `own` digest `digests.json` records.

    Args:
        rendered: The rendered tags, as they will be written.
        published: The tags the bundle publishes. No other tag gets a Kustomization, so no
            other tag gets a snapshot directory.
        timeout: The `timeout` of the `<tag>-snapshot` Kustomization, which takes the bundle's
            default (`bundle.flux_files`).
        roles: The role tree whose defaults declare the claims.

    Raises:
        RenderError: A tag's claims cannot be read, match no rendered PVC, or make a snapshot
            name too long, or the deadline margin leaves no time.
    """
    published = set(published)
    claims, problems = snapshot_claims(
        sorted(published & {t.tag for t in rendered}), roles
    )
    if problems:
        raise RenderError("; ".join(problems))
    if not any(claims.values()):
        return rendered
    deadline = seconds(timeout) - DEADLINE_MARGIN_S
    if deadline <= 0:
        raise RenderError(
            f"a Kustomization timeout of {timeout} leaves the snapshot Job no time"
        )
    volume = _defaults(SNAPSHOT_DEFAULTS)
    image = str(_defaults(FLUX_DEFAULTS)["flux_job_image"])
    script, helper = SCRIPT.read_text(), HELPER.read_text()
    attached = []
    for tag in rendered:
        if not claims.get(tag.tag):
            attached.append(tag)
            continue
        files = snapshot_files(
            tag.tag,
            role_digest8(tag),
            claim_namespaces(tag.tag, tag.files, claims[tag.tag]),
            image,
            int(volume["volume_snapshot_retain"]),
            int(volume["volume_snapshot_timeout"]),
            int(volume["volume_snapshot_poll_interval"]),
            deadline,
            script,
            helper,
        )
        attached.append(replace(tag, snapshot=files))
    return attached
