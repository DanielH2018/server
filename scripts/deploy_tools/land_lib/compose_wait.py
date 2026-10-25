"""H5.3's compose condition for the landing's host wait.

A merge that changes the files daniel-pi's package installs under `/usr/lib/homelab/compose/`
changes what `homelab-compose.service` runs. `host_wait` then also needs the unit's last run
to have passed at the merge commit or a descendant: `homelab_compose_up_result{host=...}` at 1
with a `sha` label that `host_wait.covers` accepts. A 0, an older SHA and an absent series each
mean not settled. A reconcile that passed without running the project, such as with the unit
masked, reads as an older SHA here, which `homelab_host_reconcile_ok` cannot see.
"""

from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from deploy_tools.land_lib.outcome import say

COMPOSE_ROOT = "root/usr/lib/homelab/compose/"
UP_RESULT = "homelab_compose_up_result"


def touches_compose(old, new) -> bool:
    """Whether two rendered host trees differ under the compose project's directory."""
    keys = {k for k in (*old, *new) if k.startswith(COMPOSE_ROOT)}
    return any(old.get(k) != new.get(k) for k in keys)


def compose_settled(host: str, merge: str, primary: Path, reads, covers, query) -> bool:
    """Whether `host`'s last compose run passed at `merge` or a descendant.

    Args:
        host: The inventory host name.
        merge: The merge commit's full SHA.
        primary: The primary checkout, for the ancestry check.
        reads: The `HostReads` the host wait holds.
        covers: `host_wait.covers`.
        query: `host_wait.query`.
    """
    series = query(UP_RESULT + '{host="%s"}' % host, reads)
    for sample in series:
        sha = sample["metric"].get("sha", "")
        if float(sample["value"][1]) == 1 and covers(sha, merge, primary, reads):
            return True
    say(f"{host}: no passing {UP_RESULT} at {merge[:12]} or a descendant yet")
    return False
