"""Paths, task readers and the no-cluster detector the `k8s/volume-revert` guards share.

The guards split five ways on 2026-09-01 -- the drill-proven sequence, the mutation guards,
the snapshot selection, the manifests include, and the seam and input checks that stay in
`test_volume_revert.py` -- and each reads the same task files through these.
"""

from __future__ import annotations

from pathlib import Path

from _helpers import REPO as _REPO
from _helpers import load_tasks as _tasks
from _helpers import task_named


_ROLE = _REPO / "ansible/roles/k8s/volume-revert"

_CLAIM = _ROLE / "tasks/claim.yml"

_MAIN = _ROLE / "tasks/main.yml"

_DEFAULTS = _ROLE / "defaults/main.yml"

_VALIDATOR = _REPO / "scripts/validate/k8s_manifests.py"

_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"

_GUARD = "not (k8s_no_mutate | bool)"


def _task_names(path: Path) -> list[str]:
    return [str(task.get("name", "")) for task in _tasks(path)]


def _named(path: Path, fragment: str) -> dict:
    return task_named(_tasks(path), fragment)


def _index(names: list[str], fragment: str) -> int:
    """The position of the ONE task whose name contains `fragment`.

    Unique-match, not first-match, and that is the whole point. An ordering assert built on
    first-match is satisfied by any earlier task that happens to share the substring, so
    renaming a read task to mention `disableFrontend` would make the assert below pass while
    the assert task itself sat after the revert. This repo has shipped six tests that credit
    the argument against the thing; refusing an ambiguous match is how this one avoids being
    the seventh.
    """
    hits = [i for i, name in enumerate(names) if fragment in name]
    if len(hits) != 1:
        raise AssertionError(
            f"{fragment!r} matches {len(hits)} task names in the file under test; an ordering "
            f"assert can only pin a unique task. Rename so exactly one task carries it."
        )
    return hits[0]


# kubectl's ways of saying "there is no cluster here to ask". Measured 2026-08-21: kubectl does
# NOT print the bare string "connection refused" — it prints "The connection to the server
# localhost:8080 was refused", and a cluster without Longhorn's CRDs answers "the server doesn't
# have a resource type". A guard that misses either turns "no cluster" into a red test on any
# machine that ships kubectl, GitHub's ubuntu runners included. The first four match
# `test_volume_snapshot.py`'s list.
_NO_CLUSTER = (
    "connection refused",
    "was refused",
    "i/o timeout",
    "no configuration has been provided",
    "doesn't have a resource type",
)


def _no_cluster_to_ask(stderr: str) -> bool:
    """Whether kubectl failed for want of a cluster rather than for want of a valid jsonpath.

    A rejected jsonpath never counts as unreachable, whatever else the stderr says: that is the
    one failure the seam test exists to catch, and it reads `error: error parsing jsonpath …`.
    """
    return "jsonpath" not in stderr and any(token in stderr for token in _NO_CLUSTER)


def _guard_of(task: dict) -> list[str]:
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return [str(condition).strip() for condition in conditions]
