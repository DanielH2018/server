"""Check 11 of the Longhorn backup-plane heartbeat: weekly volumes a RecurringJob skipped (#3968).

A RecurringJob leaves out any volume that is faulted or detached when it filters its volumes,
logs `Cannot create job for <vol> volume in state <state>`, never retries it, and still reports
`succeeded=1`. Check 4 sees that only once the volume's newest backup passes the 198 h weekly
limit, so a skip on a shard's normal weekday went about seven days with no recovery point and no
alert. This reads the skip from the job pod's log instead, within one 10-minute tick.

Kept apart from longhorn_backup_health_logic.py, which sits at its length cap. Same contract:
stdlib only, every input injected, no cluster needed to test it.
"""

from __future__ import annotations

import datetime as _dt
import re

# Resolves via longhorn_backup_health.py's own sys.path.insert, as the sibling logic modules do.
# host_lib.py sits beside this package, not inside it.
from host_lib import rfc3339_to_epoch

# One line of `kubectl logs --prefix`: `[pod/<pod>/<container>] time="..." level=warning
# msg="Cannot create job for <vol> volume in state <state>" ...`. The container is named for the
# RecurringJob. Longhorn writes this line from filterVolumesForJob for a faulted volume, and for
# a detached one, then leaves the volume out of that run without retrying it.
_SKIP_RE = re.compile(
    r'^\[pod/[^/\]]+/(?P<job>[^\]]+)\] time="(?P<ts>[^"]*)"'
    r'.*msg="Cannot create job for (?P<vol>\S+) volume in state \S+"'
)


def parse_skipped_volumes(log_text: str) -> list[tuple[str, str, float]]:
    """(RecurringJob, volume, skipped-at epoch) for each skip line in `log_text`.

    `log_text` is the output of `kubectl logs -l recurring-job.longhorn.io --prefix`. A line
    whose `time=` does not parse dates its skip at 0, so any backup of the volume clears it; the
    weekly staleness limit in check 4 still backs that case up.
    """
    skips = []
    for line in log_text.splitlines():
        m = _SKIP_RE.match(line)
        if m:
            skips.append((m["job"], m["vol"], rfc3339_to_epoch(m["ts"]) or 0.0))
    return skips


# DECIDED: only weekly-tier volumes are checked. A daily volume a job skipped gets its next
# chance the following night, inside check 4's daily limit, so flagging it would hold the tile
# DOWN for a day after every cold start over a gap that closes on its own. A weekly volume waits
# seven days for its next run and up to 198 h for check 4 to notice (#3968).
def check_skipped_weekly_volumes(
    skips: list[tuple[str, str, float]],
    watched: dict[str, str],
    coverage_rows: list[tuple[str, str, str]],
) -> tuple[int, str] | None:
    """Names each weekly volume a RecurringJob skipped that has no backup taken since.

    Args:
      skips: parse_skipped_volumes() output.
      watched: volume name -> namespace/pvcName, for the weekly-shard volumes on an armed
        target. A skip of a volume outside it (daily tier, deleted, disarmed) is ignored.
      coverage_rows: (volumeName, snapshotCreatedAt, RecurringJob) across every Backup. A
        backup from any job clears a skip, so a hand seed from seed_volume_backup.yml does.

    Same severity as check 4's stale-or-missing: a backed-up volume has no recent recovery point.
    """
    flagged: dict[str, str] = {}
    for job, vol, skipped_s in skips:
        if vol not in watched:
            continue
        if any(
            v == vol and (rfc3339_to_epoch(ts) or 0) > skipped_s
            for v, ts, _ in coverage_rows
        ):
            continue
        when = _dt.datetime.fromtimestamp(skipped_s, tz=_dt.timezone.utc)
        flagged[vol] = f"{watched[vol]} ({job} at {when:%Y-%m-%dT%H:%MZ})"
    if not flagged:
        return None
    return (
        3,
        "weekly volume(s) a RecurringJob skipped, no backup since "
        "(seed per docs/longhorn-disaster-recovery.md): " + ", ".join(flagged.values()),
    )
