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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's modules import each other bare, so its directory goes on the path for
# `deploy_locks`, the one naming site for the service lock files.
sys.path.insert(1, str(GITOPS_DEPLOY_FILES))
import deploy_locks
from diagnostics.probe_lib.releases import RELEASE_DIR


def _waited_out_a_deploy(name: str) -> bool:
    """True when a deploy held service lock `name`; returns once that deploy has released it.

    The lock is taken SHARED and dropped at once: it is a barrier, not a hold. A deploy takes
    its tag's lock exclusively, so a shared take succeeds only once no deploy of that service
    is running, while a second landing's gate takes nothing that would block this one. A lock
    file that cannot be opened -- no deploy of that service has ever run here -- is no evidence
    of a deploy and returns False. A wait that outlasts
    `deploy_locks.SERVICE_LOCK_WAIT_S` returns True, and the re-gate then reads the rollout
    still in progress, which fails the landing as it should.
    """
    try:
        fd = os.open(deploy_locks.lock_path(name), os.O_RDONLY)
    except OSError:
        return False
    try:
        if deploy_locks.take(fd, fcntl.LOCK_SH, time.monotonic(), 0):
            return False
        deadline = time.monotonic() + deploy_locks.SERVICE_LOCK_WAIT_S
        deploy_locks.take(fd, fcntl.LOCK_SH, deadline, deploy_locks.SERVICE_LOCK_POLL_S)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _stamped_since(service: str, since: float | None, release_dir: Path) -> bool:
    """True when `service`'s release record was stamped after `since` (wall-clock seconds).

    `release_stamp.yml` writes the record after every real apply, at one-second precision. A
    deploy of the same service cannot apply until the landing's own deploy has released the
    service lock, so a record stamped after step 5 ended came from a later deploy. An
    unreadable record, or no `since`, is no evidence and returns False.
    """
    if since is None:
        return False
    try:
        record = json.loads((release_dir / f"{service}.json").read_text())
        stamped = datetime.strptime(record["applied_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except OSError, ValueError, TypeError, KeyError:
        return False
    return stamped.timestamp() > since


def later_deploys(
    tags: list[str], since: float | None, release_dir: Path = RELEASE_DIR
) -> list[str]:
    """The tags another deploy has rolled out since `since`, once every such deploy has ended.

    A failed gate reads ONE sample. A deploy of the same service that started after this
    landing's deploy released the service lock rolls the pods under that sample, and a
    `Recreate` workload then reads `0/1 ready` mid-roll (#3812). Two signals catch it, and
    each covers the window the other misses:

    - the service lock is held NOW: a deploy is in flight. `kubectl apply` rolls a changed
      pod template before the release record is stamped, so the record alone misses this.
    - the release record was stamped after `since`: a deploy came and went between the
      gate's sample and this check, so the lock alone misses it.

    `all` is probed as well as each tag, because the tick's broad apply takes `all`
    exclusively and no tag lock. Its holder re-rolls any of the tags, so all of them count.
    `release_dir` is where the records are read, a parameter so a test can point it elsewhere.
    """
    if _waited_out_a_deploy(deploy_locks.SERVICE_LOCK_ALL):
        for tag in tags:
            _waited_out_a_deploy(tag)
        return list(tags)
    return [
        t
        for t in tags
        if _waited_out_a_deploy(t) or _stamped_since(t, since, release_dir)
    ]
