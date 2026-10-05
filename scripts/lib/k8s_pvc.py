#!/usr/bin/env python3
"""PersistentVolumeClaim names a rendered manifest declares, and the ones it references.

The validator cross-references the two sets across the whole tree, which is why the declaring
half and the referencing half are separate functions rather than one walk.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from lib.k8s_yaml import StrictKeyLoader

__all__ = [
    "find_claim_name_refs",
    "find_pvc_names",
    "parse_docs",
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
