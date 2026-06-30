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
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("FAIL|unparseable maintenance info: %s" % e); sys.exit(0)
now = datetime.now(timezone.utc)
problems = []
full = d.get("full", {}); owner = d.get("owner", ""); sched = d.get("schedule", {})
if not full.get("enabled"): problems.append("full maintenance DISABLED")
if not owner: problems.append("no maintenance owner")
nxt = sched.get("nextFullMaintenance"); overdue_h = 0.0
if nxt:
    overdue_h = (now - datetime.fromisoformat(nxt)).total_seconds() / 3600
    if overdue_h * 3600 > GRACE_S: problems.append("full maintenance overdue %.1fh" % overdue_h)
last_end = last_task = None; last_ok = True
for task, recs in (sched.get("runs") or {}).items():
    for r in recs or []:
        e = r.get("end")
        if e and (last_end is None or e > last_end):
            last_end, last_ok, last_task = e, bool(r.get("success", False)), task
if last_end and not last_ok: problems.append("most recent run (%s) FAILED" % last_task)
print(("FAIL|" + "; ".join(problems)) if problems else
      ("OK|full maint enabled, owner %s, next in %.1fh, last run %s ok" % (owner, -overdue_h, last_task or "?")))
')

MSG=$(printf '%s' "$RESULT" | cut -d'|' -f2- | tr -d '"' | tr '\n' ' ')
if [ "${RESULT%%|*}" = "OK" ]; then
  write_state true "${MSG:-maintenance ok}"
else
  write_state false "${MSG:-maintenance check failed}"
  exit 1
fi
