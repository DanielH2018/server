"""The gate Job in `<tag>-verify`: one run of a CronJob per role revision, copied from it.

Under Ansible, `k8s/cronjob-gate` runs `kubectl create job --from=cronjob/<name>` after each
deploy, so a CronJob-only role's new image runs once before the schedule reaches it. Under Flux
the renderer writes that Job instead (D5, *`cronjob-gate`'s gate Job*). A role opts a CronJob
in through `flux_cronjob_gates` in its `defaults/main.yml` (`ordering.cronjob_gates`), and
`DIR/cluster/<tag>-verify/` then holds the Job `<cronjob>-deploy-gate-<spec digest8>`:

- its spec is the CronJob's `jobTemplate.spec`, as `restart.py` stamped it, less
  `ttlSecondsAfterFinished`, which the Job census forbids because Flux re-creates a Job its
  TTL deleted;
- its pod template keeps the CronJob's labels, so the fences that admit the CronJob admit the
  gate, and gains the annotation `homelab/role-digest8`, the role's `digest8`. The spec digest
  covers it, so a role change renames the Job and Flux runs it again;
- a verify gate sets `backoffLimit: 0`, so a flaky run fails rather than passing on a retry. A
  mutation gate keeps the CronJob's own `backoffLimit` and carries `homelab/rerun-safe`.

The Job carries no ownerReference to the CronJob, whose UID is unknown at render time, so
`concurrencyPolicy: Forbid` no longer keeps a scheduled firing off a gate run. That cost and
the stricter gate (any non-zero exit fails the Kustomization) are each flip PR's to state.
`bundle.flux_files` raises the `<tag>-verify` timeout above the gate's `activeDeadlineSeconds`,
so a hung run fails on its own deadline.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import copy
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle import KUSTOMIZATION, kustomization_text
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.digest8 import job_spec_digest8
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import VERIFY_SUFFIX, cronjob_gates, split_kind
from renderer.renderer_lib.snapshot import role_digest8

# The Job census's annotations (`scripts/renderer/tests/_job_census.py`).
JOB_KIND = "homelab/job-kind"
RERUN_SAFE = "homelab/rerun-safe"
ROLE_DIGEST8 = "homelab/role-digest8"
GATE_INFIX = "-deploy-gate-"
# The Job controller names a pod `<job>-<5 characters>` and truncates the base past 58
# characters, the trailing `-` included. `pull-pi-peers.sh` recognises a gate run by its pod
# name's prefix, so a longer Job name would break that match.
MAX_NAME = 57


def gate_name(cronjob: str, spec_digest8: str) -> str:
    """The gate Job's name, `<cronjob>-deploy-gate-<spec digest8>`."""
    return f"{cronjob}{GATE_INFIX}{spec_digest8}"


def gate_job(cronjob: Mapping, digest8: str, gate: Mapping) -> dict:
    """The gate Job for `cronjob`, a rendered CronJob document.

    Args:
        cronjob: The CronJob as `cluster/<tag>/` holds it.
        digest8: The role's `digest8` (`snapshot.role_digest8`).
        gate: Its `flux_cronjob_gates` entry: `kind`, and `rerun_safe` for a mutation.

    Raises:
        RenderError: The CronJob has no pod template, a mutation gate's has no
            `backoffLimit`, or the name is longer than `MAX_NAME`.
    """
    meta = cronjob.get("metadata") or {}
    name = str(meta.get("name"))
    template = copy.deepcopy((cronjob.get("spec") or {}).get("jobTemplate") or {})
    spec = template.get("spec") or {}
    if not (spec.get("template") or {}).get("spec"):
        raise RenderError(f"CronJob {name} has no jobTemplate pod template to copy")
    spec.pop("ttlSecondsAfterFinished", None)
    pod_meta = spec["template"].setdefault("metadata", {})
    pod_meta.setdefault("annotations", {})[ROLE_DIGEST8] = digest8
    annotations = {JOB_KIND: gate["kind"]}
    if gate["kind"] == "verify":
        spec["backoffLimit"] = 0
    else:
        if "backoffLimit" not in spec:
            raise RenderError(
                f"CronJob {name}'s mutation gate copies its backoffLimit, and it sets none"
            )
        annotations[RERUN_SAFE] = gate["rerun_safe"]
    job_meta = {"name": name, "namespace": meta.get("namespace")}
    if (template.get("metadata") or {}).get("labels"):
        job_meta["labels"] = dict(template["metadata"]["labels"])
    job_meta["annotations"] = annotations
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": job_meta, "spec": spec}
    job_meta["name"] = gate_name(name, job_spec_digest8(job))
    if len(job_meta["name"]) > MAX_NAME:
        raise RenderError(
            f"gate Job {job_meta['name']} is longer than {MAX_NAME} characters, so its pods "
            "would not carry its name as a prefix"
        )
    return job


def _pod_template(template: Mapping) -> dict:
    """`template` without `ROLE_DIGEST8`, and without the metadata that leaves empty."""
    template = copy.deepcopy(dict(template))
    meta = template.get("metadata") or {}
    (meta.get("annotations") or {}).pop(ROLE_DIGEST8, None)
    if "annotations" in meta and not meta["annotations"]:
        del meta["annotations"]
    if "metadata" in template and not meta:
        del template["metadata"]
    return template


def is_gate_copy(
    job: Mapping, directory: str, bundle: Mapping[str, list[dict]]
) -> bool:
    """Whether `job` is a gate Job that `gate_job` copied from a CronJob in the bundle.

    The Job census exempts such a Job from its `delivery` and `helper` rows (#5562): it runs
    the CronJob's own image and command, so it has no mounted script for those rows to read.
    A copy sits in `<tag>-verify`, carries `homelab/role-digest8`, and its pod template less
    that annotation equals the `jobTemplate` pod template of a CronJob in `<tag>`. Any other
    edit to the pod template, such as another image, makes it an ordinary Job again.

    Args:
        job: A Job document from the bundle.
        directory: The `cluster/` directory that holds it.
        bundle: Each `cluster/` directory of the bundle, mapped to its documents.
    """
    tag, suffix = split_kind(directory)
    template = (job.get("spec") or {}).get("template") or {}
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    if suffix != VERIFY_SUFFIX or ROLE_DIGEST8 not in annotations:
        return False
    own = _pod_template(template)
    for doc in bundle.get(tag, []):
        if doc.get("kind") != "CronJob":
            continue
        spec = ((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        if own == _pod_template(spec.get("template") or {}):
            return True
    return False


def _cronjobs(files: Mapping[str, str]) -> dict[str, dict]:
    found = {}
    for text in files.values():
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") == "CronJob":
                found[str((doc.get("metadata") or {}).get("name"))] = doc
    return found


def gate_files(
    tag: RenderedTag, gates: Mapping[str, Mapping], substitutes: bool = False
) -> dict[str, str]:
    """`tag.verify` with one `<cronjob>-deploy-gate.yaml` per gate, and its kustomization.

    Args:
        tag: The rendered tag whose CronJobs the gates copy.
        gates: Its `flux_cronjob_gates` entries, by CronJob name.
        substitutes: Whether `cluster/<tag>/` holds a placeholder. Its every `$` is then
            escaped or a placeholder, and `<tag>-verify` substitutes nothing, so a copied `$`
            would reach the pod as written.

    Raises:
        RenderError: A gate names a CronJob `tag` does not render, its file name is one the
            role's `templates/verify/` already renders, or it copies a `$` from a
            substituting directory.
    """
    rendered = _cronjobs(tag.files)
    missing = sorted(set(gates) - set(rendered))
    if missing:
        raise RenderError(
            f"{tag.tag}'s flux_cronjob_gates names {missing}, which it renders no CronJob for"
        )
    digest8 = role_digest8(tag)
    files = {k: v for k, v in tag.verify.items() if k != KUSTOMIZATION}
    for name, gate in sorted(gates.items()):
        out = f"{name}-deploy-gate.yaml"
        if out in files:
            raise RenderError(f"{tag.tag}: templates/verify/ already renders {out}")
        text = yaml.safe_dump(gate_job(rendered[name], digest8, gate), sort_keys=False)
        if substitutes and "$" in text:
            raise RenderError(
                f"{tag.tag}: the gate for CronJob {name} copies a $ from cluster/{tag.tag}/, "
                "which substitutes, into a -verify directory that does not, so the pod would "
                "run the escaped text"
            )
        files[out] = text
    files[KUSTOMIZATION] = kustomization_text(list(files))
    return files


def attach_gates(
    rendered: list[RenderedTag],
    published: Iterable[str],
    roles: Path = K8S_ROLES,
    key_sets: Mapping[str, frozenset[str]] | None = None,
) -> list[RenderedTag]:
    """`rendered` with each published tag's gate Jobs added to its `<tag>-verify` directory.

    Runs after every pass that edits `cluster/<tag>/`, because the Job copies the CronJob as
    Flux applies it and embeds that directory's digest, which `digests.json` records as `own`.
    `key_sets`, a placeholder render's key set per directory, says which tags substitute.

    Raises:
        RenderError: A tag's `flux_cronjob_gates` cannot be read, or `gate_files` refuses it.
    """
    gates, problems = cronjob_gates(set(published) & {t.tag for t in rendered}, roles)
    if problems:
        raise RenderError("; ".join(problems))
    return [
        replace(
            tag,
            verify=gate_files(tag, gates[tag.tag], bool((key_sets or {}).get(tag.tag))),
        )
        if tag.tag in gates
        else tag
        for tag in rendered
    ]
