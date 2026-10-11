#!/usr/bin/env python3
"""Whether a later deploy re-rolled a landing's workloads under its health gate (#3812).

land.sh's deploy releases the service lock before step 6 gates the workloads, so a second
deploy of the same service -- another landing, or the tick -- can roll the pods under the
gate's one sample. A `Recreate` workload then reads `0/1 ready` mid-roll and a healthy change
fails the gate. `health_verdict.health` asks `later_deploys` after a failed gate and gates
again once any such deploy has ended.

Its own module beside `land_lib`, because `land_lib/tools.py` is at the 600-line cap.

Run: uv run pytest scripts/deploy_tools/tests/test_land_rerolls.py
"""

import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's modules import each other bare, so its directory goes on the path for
# `deploy_locks`, the one naming site for the service lock files.
sys.path.insert(1, str(GITOPS_DEPLOY_FILES))
import deploy_locks
from diagnostics.probe_lib.releases import RELEASE_DIR


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
    tags: list[str], since: float | None, release_dir: Path = RELEASE_DIR
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
    exclusively and no tag lock. Its holder re-rolls any of the tags, so all of them count.

    The commit is read after the waits, so it names what the LAST deploy rendered; "" when the
    record cannot say. The caller needs it because a healthy re-gate proves only that the later
    generation is healthy, not that it carries the landing's change: the tick's rollback
    redeploys an older commit. One deadline, `deploy_locks.SERVICE_LOCK_WAIT_S` from now,
    bounds every wait together. `release_dir` is a parameter so a test can point it elsewhere.
    """
    deadline = time.monotonic() + deploy_locks.SERVICE_LOCK_WAIT_S
    everything = _waited_out_a_deploy(deploy_locks.SERVICE_LOCK_ALL, deadline)
    rerolled = {}
    for tag in tags:
        held = _waited_out_a_deploy(tag, deadline)
        record = _record(tag, release_dir)
        if everything or held or _stamped_since(record, since):
            commit = record.get("commit")
            rerolled[tag] = commit if isinstance(commit, str) else ""
    return rerolled
