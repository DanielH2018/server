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

if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "gitops_tick.sh: $UNIT is not installed on $(hostname) — the GitOps deployer" >&2
  echo "runs only on hosts with has_gitops: true (daniel-box)." >&2
  exit 2
fi

# Wall clock for the journal window, monotonic for detecting a NEW activation. The
# monotonic stamp is what distinguishes "our run finished" from "the unit was already
# inactive and never started" — ActiveState alone cannot tell those apart.
since="$(date '+%Y-%m-%d %H:%M:%S')"
started_before="$(show ExecMainStartTimestampMonotonic)"

# A run already in flight is JOINED, not duplicated: systemd coalesces a start request
# for a unit that is already `activating` into the run in flight. Say so plainly, so an
# empty-looking journal is not read as a tick that did nothing.
if [[ "$(show ActiveState)" == "activating" ]]; then
  echo "A tick is already in flight (started $(show ExecMainStartTimestamp)); watching it"
  echo "instead of starting a second one — systemd coalesces the request either way."
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
  echo "Started. Read it with:"
  echo "  journalctl -u $UNIT --since '$since' --no-pager"
  exit 0
fi

echo "Waiting up to ${WAIT_S}s for it to finish..."
deadline=$((SECONDS + WAIT_S))
while [[ $SECONDS -lt $deadline ]]; do
  state="$(show ActiveState)"
  if [[ "$state" != "activating" && "$state" != "deactivating" &&
        "$(show ExecMainStartTimestampMonotonic)" != "$started_before" ]]; then
    break
  fi
  sleep 5
done

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
  echo "last_run:     $(date -d "@$last_run" '+%Y-%m-%d %H:%M:%S') ($((  $(date +%s) - last_run ))s ago)"
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
echo "─────────────────────────────────────────────────────────────────────────────"

state="$(show ActiveState)"
if [[ "$state" == "activating" || "$state" == "deactivating" ]]; then
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
if journalctl -u "$UNIT" --since "$since" --no-pager 2>/dev/null |
  grep -qF "$CONTENTION_MARKER"; then
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
