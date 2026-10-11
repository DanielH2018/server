#!/usr/bin/env python3
"""Whether a later deploy re-rolled a landing's workloads under its health gate (#3812).

land.sh's deploy releases the service lock before step 6 gates the workloads, so a second
deploy of the same service -- another landing, or the tick -- can roll the pods under the
gate's one sample. A `Recreate` workload then reads `0/1 ready` mid-roll and a healthy change
fails the gate. `health_verdict.health` asks `later_deploys` after a failed gate and gates
again once any such deploy has ended.

It reads alone. It answers that one question from the release records and the deployer's
locks, and it reads nothing from `land_lib/tools.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_rerolls.py
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's modules import each other bare, so its directory goes on the path for
# `deploy_locks`, the one naming site for the service lock files.
sys.path.insert(1, str(GITOPS_DEPLOY_FILES))
from deployer_lib import deploy_locks
from diagnostics.probe_lib.releases import RELEASE_DIR
from lib.kubectl import MissingKubectl, WrongCluster, kubectl_json

# A workload as `_workload_generations` keys it: (kind, namespace, name).
Workload = tuple[str, str, str]
# The release record's `rollouts[].kind` literals, as the API server names the kind.
_ROLLOUT_KINDS = {"deploy": "Deployment", "daemonset": "DaemonSet"}
# The label `role-labelled.yaml.j2` stamps on every object a pruning role renders.
_ROLE_LABEL = "homelab/role"


def _waited_out_a_deploy(name: str, deadline: float) -> bool:
    """True when a deploy held service lock `name`; returns once that deploy has released it.

    The lock is taken SHARED and dropped at once: it is a barrier, not a hold. A deploy takes
    its tag's lock exclusively, so a shared take succeeds only once no deploy of that service
    is running, while a second landing's gate takes nothing that would block this one. A lock
    file that cannot be opened -- no deploy of that service has ever run here -- is no evidence
    of a deploy and returns False. A wait that outlasts `deadline` (a `time.monotonic()`
    value) returns True, and the re-gate then reads the rollout still in progress, which fails
    the landing as it should.
    """
    try:
        fd = os.open(deploy_locks.lock_path(name), os.O_RDONLY)
    except OSError:
        return False
    try:
        if deploy_locks.take(fd, fcntl.LOCK_SH, time.monotonic(), 0):
            return False
        deploy_locks.take(fd, fcntl.LOCK_SH, deadline, deploy_locks.SERVICE_LOCK_POLL_S)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _record(service: str, release_dir: Path) -> dict:
    """`service`'s release record, or {} when it cannot be read."""
    try:
        record = json.loads((release_dir / f"{service}.json").read_text())
    except OSError, ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def _workload_generations() -> dict[Workload, tuple[int, str]] | None:
    """{workload: (metadata.generation, its `homelab/role` label)} across the cluster.

    `metadata.generation` moves on every change to a workload's spec, and a pod-template
    change is the only spec change that rolls its pods. None when the cluster cannot be read,
    which `later_deploys` treats as no evidence of a roll.
    """
    try:
        doc = kubectl_json(
            "prod", "get", "deployments,daemonsets,statefulsets", "-A", "-o", "json"
        )
    except MissingKubectl, WrongCluster, subprocess.TimeoutExpired, OSError:
        return None
    if doc is None:
        return None
    found: dict[Workload, tuple[int, str]] = {}
    items = doc.get("items")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            continue
        labels = meta.get("labels")
        role = labels.get(_ROLE_LABEL) if isinstance(labels, dict) else None
        generation = meta.get("generation")
        key = (str(item.get("kind")), str(meta.get("namespace")), str(meta.get("name")))
        found[key] = (
            generation if isinstance(generation, int) else 0,
            role if isinstance(role, str) else "",
        )
    return found


def _owned(
    tag: str, record: dict, workloads: dict[Workload, tuple[int, str]]
) -> set[Workload]:
    """The workloads in `workloads` that belong to `tag`.

    A workload belongs when it carries `homelab/role: <tag>`, or when the tag's release record
    names it in `rollouts[]`. The label alone misses a role that does not prune, and the
    record alone misses a workload no rollout entry names.
    """
    named = set()
    for entry in record.get("rollouts") or []:
        if isinstance(entry, dict) and entry.get("name"):
            kind = _ROLLOUT_KINDS.get(entry.get("kind") or "deploy", "")
            named.add((kind, entry.get("namespace"), entry["name"]))
    return {
        (kind, ns, name)
        for (kind, ns, name), (_gen, role) in workloads.items()
        if role == tag or (kind, ns, name) in named or (kind, None, name) in named
    }


def _generation_moved(
    tag: str,
    record: dict,
    before: dict[Workload, tuple[int, str]] | None,
    after: dict[Workload, tuple[int, str]] | None,
) -> bool:
    """True when one of `tag`'s workloads changed generation, appeared or vanished in between.

    Either read failing is no evidence and returns False.
    """
    if before is None or after is None:
        return False
    for key in _owned(tag, record, before) | _owned(tag, record, after):
        if (before.get(key) or (None,))[0] != (after.get(key) or (None,))[0]:
            return True
    return False


def _record_rolled(record: dict, previous: dict) -> bool:
    """True when the deploy that stamped `record` rolled pods, as far as the record can say.

    It rolled when it queued a restart (`rollouts[].restart`), or when it applied manifests
    that differ from the ones the record before it names. A re-apply of identical manifests
    stamps a fresh record and rolls nothing. With no previous record to compare against, a
    stamp is still counted, since nothing shows the apply was idempotent.
    """
    if any(
        isinstance(entry, dict) and entry.get("restart")
        for entry in record.get("rollouts") or []
    ):
        return True
    digest = record.get("manifests_digest")
    return not digest or digest != previous.get("manifests_digest")


def _stamped_since(record: dict, since: float | None) -> bool:
    """True when `record` was stamped after `since` (wall-clock seconds).

    `release_stamp.yml` writes the record after every real apply, at one-second precision. A
    deploy of the same service cannot apply until the landing's own deploy has released the
    service lock, so a record stamped after step 5 ended came from a later deploy. An
    unreadable stamp, or no `since`, is no evidence and returns False.
    """
    if since is None:
        return False
    try:
        stamped = datetime.strptime(
            record.get("applied_at") or "", "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
    except ValueError, TypeError:
        return False
    return stamped.timestamp() > since


def later_deploys(
    tags: list[str],
    since: float | None,
    release_dir: Path = RELEASE_DIR,
    generations: Callable[
        [], dict[Workload, tuple[int, str]] | None
    ] = _workload_generations,
) -> dict[str, str]:
    """{tag: the commit its release record names} for each tag another deploy re-rolled.

    Returns once every such deploy has ended. A failed gate reads ONE sample. A deploy of the
    same service that started after this landing's deploy released the service lock rolls the
    pods under that sample, and a `Recreate` workload then reads `0/1 ready` mid-roll (#3812).
    Two signals catch it, and each covers the window the other misses:

    - the service lock is held NOW: a deploy is in flight. `kubectl apply` rolls a changed
      pod template before the release record is stamped, so the record alone misses this.
    - the release record was stamped after `since`: a deploy came and went between the
      gate's sample and this check, so the lock alone misses it.

    `all` is probed as well as each tag, because the tick's broad apply takes `all`
    exclusively and no tag lock. Its holder may reach any of the tags.

    NEITHER SIGNAL COUNTS ON ITS OWN (#4514). A held lock proves a deploy ran, not that it
    changed this tag's pods: a broad apply that never touches the tag, or an idempotent
    re-apply, rolls nothing, and a re-gate would then settle a failed gate on the evidence the
    first gate already rejected. So a held lock counts only when one of the tag's workloads
    changed `metadata.generation` across the wait, read from the cluster before and after it.
    A stamp counts only when the record shows a roll (`_record_rolled`). The deploy in flight
    stamps its record before it releases the lock, so a roll it applied before the first
    generation read still reaches the stamp signal.

    The commit is read after the waits, so it names what the LAST deploy rendered; "" when the
    record cannot say. The caller needs it because a healthy re-gate proves only that the later
    generation is healthy, not that it carries the landing's change: the tick's rollback
    redeploys an older commit. One deadline, `deploy_locks.SERVICE_LOCK_WAIT_S` from now,
    bounds every wait together. `release_dir` is a parameter so a test can point it elsewhere.
    """
    deadline = time.monotonic() + deploy_locks.SERVICE_LOCK_WAIT_S
    before = generations()
    everything = _waited_out_a_deploy(deploy_locks.SERVICE_LOCK_ALL, deadline)
    held = {tag: _waited_out_a_deploy(tag, deadline) for tag in tags}
    after = generations() if everything or any(held.values()) else before
    rerolled = {}
    for tag in tags:
        record = _record(tag, release_dir)
        moved = (everything or held[tag]) and _generation_moved(
            tag, record, before, after
        )
        stamped = _stamped_since(record, since) and _record_rolled(
            record, _record(f"{tag}.previous", release_dir)
        )
        if moved or stamped:
            commit = record.get("commit")
            rerolled[tag] = commit if isinstance(commit, str) else ""
    return rerolled
