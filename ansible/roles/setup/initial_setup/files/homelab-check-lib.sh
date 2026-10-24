#!/usr/bin/env bash
# Shared host-check helpers for every host's checks -- managed by Ansible (initial_setup role);
# edits overwritten. Sourced from /usr/local/lib/homelab-check-lib.sh; not executed directly.
#
# Consumers source it with `|| exit 1`, so a host that misses it fails its cron loudly instead of
# reporting green. Its copy task is `tags: [always]` for the same reason kuma-push-lib.sh's is.
# docs/target-architecture/monitoring/m6-retire-kuma.md (*B0, B5, B7 and B8*) gives the plan.
# kuma-push-lib.sh keeps its own copies of the three functions below until the last consumer
# switches its `source` line, and C-3a then deletes that file.

# boot_grace_active GRACE_S TAG
# True (exit 0) when the host booted less than GRACE_S ago, so the caller skips this run.
# Fails OPEN: an unreadable /proc/uptime returns non-zero, so the caller runs its check.
boot_grace_active() {
  local grace="$1" tag="$2" up
  up=$(cut -d. -f1 </proc/uptime 2>/dev/null) || return 1
  [ -n "$up" ] || return 1
  [ "$up" -lt "$grace" ] || return 1
  logger -t "$tag" "boot grace: uptime ${up}s < ${grace}s — skipped, dead-man grace covers it"
  return 0
}

# wan_reachable TAG URL...
# True (exit 0) as soon as one URL answers; false only when EVERY one failed. No URL at all
# returns true, because a gate that cannot probe must report a fault, not suppress one.
wan_reachable() {
  local tag="$1"
  shift
  [ "$#" -gt 0 ] || return 0
  local url
  for url in "$@"; do
    curl -fs --max-time 5 -o /dev/null "$url" 2>/dev/null && return 0
  done
  logger -t "$tag" "WAN probe: no endpoint answered ($*)"
  return 1
}

# reachout_verdict TAG WAN_URL...
# Sets REACHOUT_STATUS to `down` when the WAN answers (the source refused: a real fault) and to
# `up` when no endpoint answers (the WAN check owns that page). REACHOUT_NOTE is the prefix the
# caller puts before its own message, empty on the `down` path.
reachout_verdict() {
  local tag="$1"
  shift
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  REACHOUT_STATUS=down
  # shellcheck disable=SC2034  # read by the sourcing script, not by this file
  REACHOUT_NOTE=""
  if ! wan_reachable "$tag" "$@"; then
    # shellcheck disable=SC2034  # read by the sourcing script, not by this file
    REACHOUT_STATUS=up
    # shellcheck disable=SC2034  # read by the sourcing script, not by this file
    REACHOUT_NOTE="skipped: WAN unreachable — "
  fi
}

# write_check_textfile CHECK SUCCESS VERDICT_TS DEADLINE_S [LINE...]
# M3's producer metric contract (docs/target-architecture/monitoring/m3-textfile-timer.md): writes
# $TEXTFILE_DIR/CHECK.prom with the four homelab_check_* series, then each LINE verbatim as a
# detail series. TEXTFILE_DIR defaults to the real path, so a test needs no root.
# homelab_check_duration_seconds is CHECK_DURATION_S when the caller sets it, else bash's $SECONDS,
# which counts from the start of the sourcing script: a cron or timer script's run time up to its
# verdict, with no change to the caller (#6166). Atomic: mktemp's
# suffix never ends in .prom, chmod 0644 because mktemp makes 0600 and the collector reads as
# uid 65534, then one mv. Returns non-zero on failure and removes its temp file; the caller logs.
write_check_textfile() {
  local check="$1" success="$2" ts="$3" deadline="$4" duration="${CHECK_DURATION_S:-$SECONDS}" dir tmp
  shift 4
  dir=${TEXTFILE_DIR:-/var/lib/node-exporter-textfile}
  tmp=$(mktemp "$dir/$check.prom.XXXXXX" 2>/dev/null) || return 1
  {
    echo "# HELP homelab_check_success 1 when the check's last verdict was up, 0 when down."
    echo "# TYPE homelab_check_success gauge"
    echo "homelab_check_success{check=\"$check\"} $success"
    echo "# HELP homelab_check_last_run_timestamp_seconds Unix time the check's last verdict was taken."
    echo "# TYPE homelab_check_last_run_timestamp_seconds gauge"
    echo "homelab_check_last_run_timestamp_seconds{check=\"$check\"} $ts"
    echo "# HELP homelab_check_deadline_seconds Longest legitimate gap between two of the check's writes."
    echo "# TYPE homelab_check_deadline_seconds gauge"
    echo "homelab_check_deadline_seconds{check=\"$check\"} $deadline"
    echo "# HELP homelab_check_duration_seconds Seconds the check's run took to reach its verdict."
    echo "# TYPE homelab_check_duration_seconds gauge"
    echo "homelab_check_duration_seconds{check=\"$check\"} $duration"
    [ "$#" -eq 0 ] || printf '%s\n' "$@"
  } >"$tmp" || {
    rm -f "$tmp"
    return 1
  }
  if ! { chmod 0644 "$tmp" && mv "$tmp" "$dir/$check.prom"; }; then
    rm -f "$tmp"
    return 1
  fi
}
