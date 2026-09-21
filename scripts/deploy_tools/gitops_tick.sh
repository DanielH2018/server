#!/usr/bin/env bash
#
# gitops_tick.sh — trigger a GitOps deploy tick by hand and report what it did.
#
# The timer (gitops_deploy_tick_interval, 10 min) does nothing but activate gitops-deploy.service, so starting that
# unit runs the identical code path: fetch origin/master, CI-gate, ff-merge, deploy the
# changed services, health-gate, roll back on failure. There is no dry-run mode — a
# manual tick is a real tick.
#
# Why a wrapper rather than plain `systemctl start`:
#   - The unit is Type=oneshot with TimeoutStartSec=45min, so a blocking start can hang
#     far past any caller's patience (a Claude Code Bash call is capped at 10 minutes).
#     This starts it with --no-block and then waits on its own bounded budget.
#   - `journalctl -f` never returns. This prints exactly the journal for the run it
#     triggered, then exits with that run's status.
#
# Requires the polkit rule from roles/setup/gitops_deploy (50-gitops-deploy.rules.j2),
# which lets the deploy user start this one unit without root. Without it systemd
# refuses with "Interactive authentication required".
#
# Usage:
#   ./scripts/deploy_tools/gitops_tick.sh              # trigger, wait up to 540s, print the journal
#   ./scripts/deploy_tools/gitops_tick.sh --wait 900   # a longer budget (a k8s rollback can need it)
#   ./scripts/deploy_tools/gitops_tick.sh --no-wait    # trigger and return immediately
#
# Exit codes:
#   0   the tick ran to completion (which includes a healthy noop / deferral)
#   1   the tick failed — the unit exited non-zero, or it could not be started
#   2   the unit is not installed on this host (has_gitops is false here)
#   4   `--no-wait` only: a tick was already in flight, so the request JOINED it and started
#       nothing. That tick fetched before this request arrived, so a commit merged since is
#       not in it, and nothing converges the checkout onto that commit until the next tick.
#       A caller that needs its own commit fast-forwarded re-runs once the run ends. With a
#       wait budget the wrapper does that itself: it watches the joined run, and when that
#       run ends cleanly it starts a FRESH run on the same budget and exits by the fresh
#       run's outcome (issue #1879). A joined run that failed or hit contention is graded
#       as itself.
#   3   the tick was skipped for lock contention — the unit's `flock -E 75` fired and
#       `SuccessExitStatus=75` makes systemd call that a success. Nothing deployed,
#       nothing failed, and nothing alerted. Detected from the unit's ExecStopPost
#       journal marker, not from the exit code, which systemd discards when a oneshot
#       unit goes inactive. NOT 75: this script already uses 75 for its own wait
#       budget, and the two mean opposite things about the run.
#   75  the wait budget elapsed while the run was still in flight (nothing is wrong
#       with the run; only this script gave up watching). Matches deploy.sh's use of
#       75 for "we backed off, no verdict".
set -euo pipefail

UNIT="gitops-deploy.service"
WAIT_S=540

# The monotonic clock systemd's start stamp is measured against. A variable rather than a
# literal so a test can point it at a fixture holding a known uptime and assert an exact
# number of seconds in flight: deriving the stamp from the REAL uptime cannot work on a
# machine that has been up for less than the window under test, which is every fresh CI
# runner. Nothing on a host ever sets it.
UPTIME_SOURCE="${GITOPS_TICK_UPTIME_SOURCE:-/proc/uptime}"

# How often `watch_run` asks systemd whether the run in flight has ended. A variable for the
# same reason as UPTIME_SOURCE: the test drives this script against a stubbed systemctl whose
# run ends within a second, and a fixed 5s poll made each of its wait-mode cases cost more
# than the whole rest of its module (#2226). Nothing on a host ever sets it; 5s because a
# healthy tick takes about five and `systemctl show` on every second would be noise.
POLL_S="${GITOPS_TICK_POLL_S:-5}"

# Emitted by the unit's ExecStopPost when `flock -E 75` fired. Must stay identical to the
# phrase in roles/setup/gitops_deploy/templates/gitops-deploy.service.j2 — the exit code is
# unreadable after a oneshot unit goes inactive, so this string is the whole signal.
# ansible/tests/deploy/test_gitops_manual_trigger.py asserts the two match.
CONTENTION_MARKER="tick skipped (lock contention)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait)
      WAIT_S="${2:?--wait needs a number of seconds}"
      shift 2
      ;;
    --no-wait)
      WAIT_S=0
      shift
      ;;
    -h | --help)
      awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"
      exit 0
      ;;
    *)
      echo "gitops_tick.sh: unknown argument '$1'" >&2
      exit 1
      ;;
  esac
done

show() { systemctl show "$UNIT" -p "$1" --value; }

# How long the run in flight has been going, from systemd's monotonic start stamp (in
# microseconds) against UPTIME_SOURCE. Both count from boot, so on a host that does not
# suspend they are the same clock. 0 when either read is unusable: the number only decorates
# a log line, and losing it must not end the tick.
in_flight_seconds() {
  local mono_us="$1" seconds=""
  if [[ "$mono_us" =~ ^[0-9]+$ ]]; then
    seconds=$(awk -v m="$mono_us" \
      '{ d = $1 - m / 1000000; if (d < 0) d = 0; printf "%d\n", d }' \
      "$UPTIME_SOURCE" 2>/dev/null || true)
  fi
  # `:-0` covers the awk that SUCCEEDS and prints nothing, which is what an empty /proc/uptime
  # gives: its action block never runs. An empty answer renders the joined line as
  # `already s in flight`, which land.py's parser used to stop matching -- the wait would then
  # go unbooked and `lock` would read 0 again, the exact defect this line exists to end.
  echo "${seconds:-0}"
}

if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "gitops_tick.sh: $UNIT is not installed on $(hostname) — the GitOps deployer" >&2
  echo "runs only on hosts with has_gitops: true (daniel-box)." >&2
  exit 2
fi

# Wall clock for the journal window, monotonic for detecting a NEW activation. The
# monotonic stamp is what distinguishes "our run finished" from "the unit was already
# inactive and never started" — ActiveState alone cannot tell those apart.
since="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
started_before="$(show ExecMainStartTimestampMonotonic)"

# A run already in flight is JOINED, not duplicated: systemd coalesces a start request
# for a unit that is already `activating` into the run in flight. Say so plainly, so an
# empty-looking journal is not read as a tick that did nothing.
joined=0
joined_after=0
if [[ "$(show ActiveState)" == "activating" ]]; then
  echo "A tick is already in flight (started $(show ExecMainStartTimestamp)); a second start"
  echo "request would be coalesced into it by systemd, so none is made."
  # Read before `started_before` is overwritten below: it IS the joined run's stamp.
  joined=1
  joined_after="$(in_flight_seconds "$started_before")"
  since="$(show ExecMainStartTimestamp | cut -d' ' -f2-3)"
  # The stamp read above IS the joined run's, so the wait loop's "a new activation
  # happened" test could never pass for it and the loop ran to its deadline however early
  # the run finished — land.sh sat out the full 540s on a broad tick another session's
  # merge had started (2026-09-01). For a joined run the state check alone decides.
  started_before="joined"
else
  echo "Triggering $UNIT on $(hostname)..."
  if ! systemctl start --no-block "$UNIT"; then
    echo >&2
    echo "gitops_tick.sh: could not start $UNIT. If that failed with 'Interactive" >&2
    echo "authentication required', the polkit rule is missing — apply it with:" >&2
    echo "  uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy" >&2
    exit 1
  fi
fi

if [[ "$WAIT_S" -eq 0 ]]; then
  if [[ "$joined" == 1 ]]; then
    # Exit 4, not 0: the in-flight tick fetched origin BEFORE this request, so a commit merged
    # since is not in it and the checkout stays behind until the next tick. land.sh's kick
    # read 0 here as "the primary converges while the gate runs" and it did not (issue #1843).
    echo "Nothing started: the run in flight (already ${joined_after}s) fetched before this"
    echo "request, so a commit merged since is not in it. Re-run once it ends, or wait for"
    echo "the timer. Follow it with:"
    echo "  journalctl -u $UNIT --since '$since' --no-pager"
    exit 4
  fi
  echo "Started. Read it with:"
  echo "  journalctl -u $UNIT --since '$since' --no-pager"
  exit 0
fi

# Watch the unit until the run in flight ends or `deadline` (in $SECONDS terms) passes. A
# run has ended when the unit is neither activating nor deactivating AND its start stamp is
# not `started_before`; the stamp is what tells "our run finished" from "the unit never
# started". Every caller compares the two afterwards for its own verdict.
watch_run() {
  local deadline="$1" state
  while [[ $SECONDS -lt $deadline ]]; do
    state="$(show ActiveState)"
    if [[ "$state" != "activating" && "$state" != "deactivating" &&
          "$(show ExecMainStartTimestampMonotonic)" != "$started_before" ]]; then
      break
    fi
    sleep "$POLL_S"
  done
}

# Whether the run whose journal starts at `since` ended on the contention marker. Checked by
# journal marker rather than exit code; the block ahead of the final verdict says why.
ended_in_contention() {
  journalctl -u "$UNIT" --since "$since" --no-pager 2>/dev/null |
    grep -qF "$CONTENTION_MARKER"
}

echo "Waiting up to ${WAIT_S}s for it to finish..."
wait_started=$SECONDS
deadline=$((SECONDS + WAIT_S))
watch_run "$deadline"
waited=$((SECONDS - wait_started))

# Neither wait is visible anywhere else in a landing: a joined tick and a slow one both exit
# 0, and land_lib/landing.py:retry_while_locked books a wait only when an attempt exits 75.
# land.py parses the JOINED line into the landing's `lock=` field
# (land_lib/tools.py:in_flock_wait). The self-started line is for an operator only: those
# seconds are the tick's own work, and the path that makes them long ends at exit 3, which
# the landing already books. 60s because a healthy tick takes about five.
if [[ "$joined" == 1 ]]; then
  echo "gitops_tick: joined a tick already ${joined_after}s in flight; waited ${waited}s for it" >&2
elif [[ "$waited" -ge 60 ]]; then
  echo "gitops_tick: waited ${waited}s" >&2
fi

# A joined run that ENDED is not this request's tick. It fetched origin before the request
# arrived, so a commit merged since is not in it, and the markers it leaves describe a tree
# that predates the caller's change: `behind_since` still set, no `broad_applied` for the
# caller's plane. land.sh read those as `deferred` or `needs-manual-apply` for work the next
# timer tick applied a minute later (issue #1879, the wait-mode half of #1843). So once the
# joined run ends cleanly, START A FRESH RUN and grade that one instead: the remaining budget
# covers it, and a healthy tick takes about five seconds. A joined run that FAILED or hit
# contention is graded as itself below -- a fresh run after a failure would skip on the hold
# it just wrote and read green over a fault the caller has to see, and after contention the
# lock is still held, which the caller's own retry loop handles.
fresh=0
if [[ "$joined" == 1 && "$WAIT_S" -gt 0 ]]; then
  state="$(show ActiveState)"
  if [[ "$state" != "activating" && "$state" != "deactivating" &&
        "$(show Result)" == "success" && "$(show ExecMainStatus)" == "0" ]] &&
     ! ended_in_contention; then
    echo "The joined run ended after ${waited}s; it fetched before this request, so a fresh"
    echo "run is started and graded instead."
    # Re-stamped for the fresh run: `since` bounds the journal and the contention grep
    # below, and a stamp left at the joined run's start would grade the fresh run by the
    # joined run's lines. `started_before` goes back to a REAL monotonic stamp -- the
    # joined run's -- so the watch can see the new activation replace it.
    since="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    started_before="$(show ExecMainStartTimestampMonotonic)"
    if ! systemctl start --no-block "$UNIT"; then
      echo "gitops_tick.sh: could not start $UNIT for the fresh run." >&2
      exit 1
    fi
    fresh=1
    watch_run "$deadline"
    echo "gitops_tick: joined run ended; started a fresh run and waited $((SECONDS - wait_started - waited))s for it" >&2
  fi
fi

echo
echo "── journal ──────────────────────────────────────────────────────────────────"
journalctl -u "$UNIT" --since "$since" --no-pager
echo "─────────────────────────────────────────────────────────────────────────────"

# An uneventful tick logs NOTHING — the deployer prints only on a deferral, an alert or a
# real deploy — so the journal alone renders a healthy run as "-- No entries --", which
# reads like the unit never ran. These three markers are the deployer's own state, and
# they distinguish "ticked, nothing to do" from "did not tick at all".
state_dir=/var/lib/gitops-deploy
echo
echo "── deployer state ───────────────────────────────────────────────────────────"
if [[ -r "$state_dir/last_run" ]]; then
  last_run="$(cut -d. -f1 <"$state_dir/last_run")"
  echo "last_run:     $(date -u -d "@$last_run" '+%Y-%m-%d %H:%M:%S UTC') ($((  $(date -u +%s) - last_run ))s ago)"
else
  echo "last_run:     unreadable — the tick did not get far enough to write it"
fi
if [[ -s "$state_dir/hold_sha" ]]; then
  echo "hold_sha:     $(cat "$state_dir/hold_sha")  <-- a rolled-back SHA is held; see the role CLAUDE.md"
else
  echo "hold_sha:     empty (no rollback is being held)"
fi
if [[ -s "$state_dir/behind_since" ]]; then
  echo "behind_since: $(cat "$state_dir/behind_since")  <-- parked behind origin since this SHA/time"
else
  echo "behind_since: empty (converged with origin)"
fi
if [[ -s "$state_dir/contention_since" ]]; then
  echo "contention:   $(cat "$state_dir/contention_since")  <-- consecutive ticks deferred on a busy service lock (sha lock first last count)"
fi
echo "─────────────────────────────────────────────────────────────────────────────"

state="$(show ActiveState)"
# The fresh run's start is asynchronous (`--no-block`), so at the deadline it may not have
# replaced the joined run's stamp yet: the unit reads `inactive` with the JOINED run's
# Result, which would grade the fresh run by a run that is not it. An unchanged stamp means
# the fresh run was never seen to start, and that is "still in flight" for this script.
if [[ "$state" == "activating" || "$state" == "deactivating" ]] ||
   [[ "$fresh" == 1 && "$(show ExecMainStartTimestampMonotonic)" == "$started_before" ]]; then
  echo
  echo "Still running after ${WAIT_S}s — the run is fine, this script stopped watching."
  echo "Follow it with: journalctl -u $UNIT --since '$since' --no-pager"
  exit 75
fi

result="$(show Result)"
status="$(show ExecMainStatus)"
echo

# Contention is checked BEFORE the success gate, and by journal marker rather than exit code.
# The unit sets `flock -E 75` + SuccessExitStatus=75, so contention leaves the unit successful
# and never `failed`. The exit code itself is not recoverable afterwards: gitops-deploy.service
# is Type=oneshot with no RemainAfterExit, and systemd resets ExecMainStatus to 0 once such a
# unit goes inactive (measured 2026-08-23, systemd 255.4). So a contention tick and a real
# deploy both read back `Result=success ExecMainStatus=0`, and only the unit's ExecStopPost
# marker distinguishes them. A genuinely failed unit is different — it stays in `failed`, which
# is why the branch below can still trust $status.
if ended_in_contention; then
  echo "Tick did not run: another holder had /var/lock/server-git-tree.lock for the"
  echo "unit's full flock wait. Nothing was deployed and last_run is untouched."
  echo "No alert fires for this — OnFailure cannot fire on a unit systemd considers"
  echo "successful, and GitOps-Alive only pages once last_run passes GITOPS_MAX_AGE_S"
  echo "(90 min). Re-run once the other deploy or secret-rotate cron finishes."
  exit 3
fi

if [[ "$result" == "success" && "$status" == "0" ]]; then
  echo "Tick completed (Result=$result, exit=$status)."
  echo "A noop or a deferral (dirty tree, ci_pending, hold) also completes successfully —"
  echo "read the journal above for which one this was."
  exit 0
fi

echo "Tick FAILED (Result=$result, exit=$status). gitops-deploy-alert.service has already" >&2
echo "posted to Discord via OnFailure." >&2
exit 1
