#!/usr/bin/env bash
# Daily kopia FULL-maintenance freshness check — managed by Ansible (kopia role); edits overwritten.
#
# Full maintenance is what actually GCs expired blobs from B2. The entrypoint idempotently asserts
# `kopia maintenance set --owner me --enable-full true`, but nothing MONITORED that it keeps
# succeeding — a stalled full cycle (lock contention, an ownership conflict after a fresh-host
# reconnect, a B2 transaction error) lets blobs accumulate and only surfaces weeks later as the
# B2-usage SYMPTOM alert (and headroom is thin). This queries `kopia maintenance info --json` and
# flags: full disabled, no owner, the next full run overdue past a grace, or the newest maintenance
# run failed. Read-only against the already-connected repo inside the running container.
#
# Reporting: writes {"ts","ok","msg"} to STATE; monitor-bridge's `maintenance` check reads it
# (read-only bind mount) and pushes the "Backup Maintenance" Kuma monitor every cycle.
set -uo pipefail

STATE=/var/lib/kopia-maintenance/state.json
GRACE_S=$((36 * 3600))   # next full run overdue by > this = a real stall (interval ~24h + slack)

write_state() { # ok msg
  # jq, not printf: a stray backslash/control char in the kopia-derived msg would make a
  # hand-built string invalid JSON -> monitor-bridge reads "state unparseable" (false DOWN).
  jq -nc --argjson ts "$(date +%s)" --argjson ok "$1" --arg msg "$2" \
    '{ts: $ts, ok: $ok, msg: $msg}' > "$STATE"
  logger -t kopia-maintenance "$1: $2"
}

OUT=$(docker exec kopia kopia maintenance info --json 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
  write_state false "maintenance info query failed (exit $RC): $(printf '%s' "$OUT" | tail -1 | tr -d '"' | tr '\n' ' ')"
  exit 1
fi

# Parse with the host python3 (kopia's image has none). JSON arrives on stdin; the program is in
# -c (single-quoted, so it contains only double quotes). Prints "OK|msg" or "FAIL|msg".
RESULT=$(printf '%s' "$OUT" | GRACE_S="$GRACE_S" python3 -c '
import json, os, sys
from datetime import datetime, timezone
GRACE_S = float(os.environ.get("GRACE_S", 129600))
# Wrap the WHOLE body, not just json.load: datetime.fromisoformat() on the kopia nanosecond+offset
# timestamp (and the runs iteration) could raise on an older-Python DR host or a future kopia
# format change. An unguarded raise exits with EMPTY stdout, and the bash wrapper reads that as
# RESULT != "OK" -> write_state false -> a FALSE Backup-Maintenance DOWN/page. Degrade any error
# to a descriptive FAIL (exit 0) instead — mirrors the old json.load handler.
try:
    d = json.load(sys.stdin)
    now = datetime.now(timezone.utc)
    problems = []
    full = d.get("full", {}); owner = d.get("owner", ""); sched = d.get("schedule", {})
    if not full.get("enabled"): problems.append("full maintenance DISABLED")
    if not owner: problems.append("no maintenance owner")
    nxt = sched.get("nextFullMaintenance"); overdue_h = 0.0
    if nxt:
        overdue_h = (now - datetime.fromisoformat(nxt)).total_seconds() / 3600
        if overdue_h * 3600 > GRACE_S: problems.append("full maintenance overdue %.1fh" % overdue_h)
    # Evaluate the full and quick task families SEPARATELY. Kopia names every full-cycle
    # subtask with a "full-" prefix (full-delete-blobs, full-drop-deleted-content,
    # full-rewrite-contents); everything else (snapshot-gc, advance-epoch, ...) runs on
    # the quick cycle. Taking the single newest run across ALL tasks (the old logic)
    # masked a FAILED full run whenever a newer quick run then succeeded -- the watchdog
    # stayed green while blob GC silently stopped.
    def latest_run(keys):
        end = task = None; ok = True
        for k in keys:
            for r in (sched.get("runs") or {}).get(k) or []:
                e = r.get("end")
                if e and (end is None or e > end):
                    end, ok, task = e, bool(r.get("success", False)), k
        return end, ok, task
    all_tasks = list((sched.get("runs") or {}).keys())
    full_end, full_ok, full_task = latest_run([k for k in all_tasks if k.startswith("full-")])
    quick_end, quick_ok, quick_task = latest_run([k for k in all_tasks if not k.startswith("full-")])
    if full_end and not full_ok: problems.append("most recent FULL task (%s) FAILED" % full_task)
    if quick_end and not quick_ok: problems.append("most recent quick task (%s) FAILED" % quick_task)
    print(("FAIL|" + "; ".join(problems)) if problems else
          ("OK|full maint enabled, owner %s, next in %.1fh, last full %s ok, last quick %s ok"
           % (owner, -overdue_h, full_task or "?", quick_task or "?")))
except Exception as e:
    print("FAIL|maintenance info check error: %s" % e); sys.exit(0)
')

MSG=$(printf '%s' "$RESULT" | cut -d'|' -f2- | tr -d '"' | tr '\n' ' ')
if [ "${RESULT%%|*}" = "OK" ]; then
  write_state true "${MSG:-maintenance ok}"
else
  write_state false "${MSG:-maintenance check failed}"
  exit 1
fi
