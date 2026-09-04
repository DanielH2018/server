#!/usr/bin/env python3
"""PersistentVolumeClaim names a rendered manifest declares, and the ones it references.

Split out of ``scripts/validate/k8s_manifests.py`` on 2026-09-04; that module re-exports every
name here, so an existing importer keeps working. The validator cross-references the two sets
across the whole tree, which is why the declaring half and the referencing half are separate
functions rather than one walk.

``volume_claim_pvc_names`` takes its Jinja environment as a parameter: the caller already has
one, and a leaf module here never imports back from the validator it was split out of.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from jinja2 import Environment

from lib import yaml_fast
from lib.k8s_yaml import StrictKeyLoader
from lib.repo_paths import K8S_ROLES

__all__ = [
    "find_claim_name_refs",
    "find_pvc_names",
    "parse_docs",
    "volume_claim_pvc_names",
]


def parse_docs(rendered: str) -> list:
    """Parse a rendered manifest into its YAML documents, the same way yaml_error does.

    Only called after yaml_error has already confirmed the render is valid YAML — a raise here would
    be a bug in this function, not in the manifest.
    """
    return list(yaml.load_all(rendered, Loader=StrictKeyLoader))


def find_pvc_names(doc) -> list[str]:
    """Return the name of the PVC `doc` declares, if it is one.

    A rendered manifest is one object per document, so this is a direct check, not a
    recursive search.
    """
    if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
        name = (doc.get("metadata") or {}).get("name")
        if isinstance(name, str):
            return [name]
    return []


def find_claim_name_refs(node) -> list[str]:
    """Every `persistentVolumeClaim.claimName` in a parsed manifest, wherever it is nested.

    A Deployment/DaemonSet has it at spec.template.spec.volumes[]; a CronJob one level deeper
    through spec.jobTemplate; a bare Pod at spec.volumes[] directly. Walked generically instead
    of hardcoded per-kind paths, so a shape this wasn't written for (a future StatefulSet, say)
    is still covered rather than silently skipped.
    """
    refs: list[str] = []
    if isinstance(node, dict):
        pvc = node.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and isinstance(pvc.get("claimName"), str):
            refs.append(pvc["claimName"])
        for value in node.values():
            refs.extend(find_claim_name_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(find_claim_name_refs(item))
    return refs


def volume_claim_pvc_names(role: str, ctx: dict, env: Environment) -> list[str]:
    """PVC names volume-claim creates on this role's behalf.

    `env` renders the claim name; the caller supplies one loading the shared templates
    (`make_env([SHARED_TPL])`) so this module needs nothing from the validator that calls it.

    volume-claim is in SKIP_ROLES and never rendered under its own role — its templates/pvc.yaml.j2
    (metadata.name: `{{ volume_claim_name }}`) only ever renders with vars a CALLING role passes
    on the `include_role` task (e.g. tdarr's `volume_claim_name: "{{ tdarr_k8s_configs_claim }}"`),
    which is otherwise invisible to this validator — it reads role defaults/templates, not
    task-level `vars:` overrides. Without this, every volume-claim-backed claimName (tdarr,
    freshrss, ...) would show as unresolved. Best-effort: only handles a plain string `vars:`
    value, which is the only form any current caller uses.
    """
    names: list[str] = []
    tasks_dir = K8S_ROLES / role / "tasks"
    if not tasks_dir.is_dir():
        return names
    for task_file in sorted(tasks_dir.glob("*.yml")):
        try:
            tasks = yaml_fast.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            inc = task.get("ansible.builtin.include_role")
            if not isinstance(inc, dict) or inc.get("name") != "k8s/volume-claim":
                continue
            claim = (task.get("vars") or {}).get("volume_claim_name")
            if not isinstance(claim, str):
                continue
            try:
                names.append(env.from_string(claim).render(ctx))
            except Exception:  # noqa: S112 -- a claim name this stub context cannot render is not one this validator can check
                continue
    return names
