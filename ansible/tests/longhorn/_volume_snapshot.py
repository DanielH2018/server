"""Paths and the task reader the `k8s/volume-snapshot` guards share.

The guards split three ways on 2026-09-01 -- retention and naming, the maintenance-attach
path, and the deploy-hygiene checks -- and each reads the same three task files.
"""

from __future__ import annotations

from pathlib import Path

from _helpers import K8S_ROLES
from _helpers import load_tasks as _tasks
from _helpers import task_named


_ROLE = K8S_ROLES / "volume-snapshot"

_CLAIM = _ROLE / "tasks/claim.yml"

_MAIN = _ROLE / "tasks/main.yml"

_DEFAULTS = _ROLE / "defaults/main.yml"

_MANIFESTS = _ROLE.parent / "manifests/tasks/main.yml"

_GUARD = "not (k8s_no_mutate | bool)"


def _named(path: Path, fragment: str) -> dict:
    return task_named(_tasks(path), fragment)
