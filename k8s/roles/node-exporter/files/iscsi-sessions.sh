#!/bin/sh
# The node's iSCSI sessions, written as a node_exporter textfile check file (#5826).
#
# A replaced Longhorn instance-manager leaves the sessions it served behind in the kernel, in
# state FREE with a failed connection. The next attach of such a volume on that node fails its
# frontend init on `iscsiadm --logout` (error 32), and Longhorn's
# auto-delete-pod-when-volume-detached-unexpectedly deletes the pod every ~30s, so the cause
# hides behind pod churn. Nothing else reads the session state: the Volume CR and the pods say
# nothing until an attach fails. docs/longhorn-disaster-recovery.md, "A stale iSCSI session
# blocks an attach", has the cleanup.
#
# It runs as the `iscsi-sessions` sidecar of the node-exporter DaemonSet, whose template
# inlines this file. It reads sysfs, which every uid may read, and writes into the emptyDir the
# exporter reads as a second textfile directory. The file follows the check-file half of M3's
# producer metric contract (docs/target-architecture/monitoring/m3-textfile-timer.md).
#
# Environment, all optional: SYSFS (default /host/sys), TEXTFILE_DIR (default /iscsi-textfile),
# INTERVAL_S (default 60) and ONCE (set to 1 to write one file and exit, for the test).
# POSIX sh, because the image's shell is busybox ash.
set -u

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

SYSFS=${SYSFS:-/host/sys}
TEXTFILE_DIR=${TEXTFILE_DIR:-/iscsi-textfile}
INTERVAL_S=${INTERVAL_S:-60}
ONCE=${ONCE:-0}
CHECK=iscsi-sessions
# The longest legitimate gap between two writes: five intervals. The IscsiSessionsNotReporting
# rules in observability's host-checks.yaml carry it as a literal, and
# ansible/roles/k8s/node-exporter/tests/test_iscsi_sessions.py pins the two together.
DEADLINE_S=$((INTERVAL_S * 5))

# One label value as read from sysfs: the file's first line, or `unknown` when it is missing.
# A sysfs attribute holds no quote or backslash, so no escaping is needed.
read_attr() {
  value=$(head -n 1 "$1" 2>/dev/null) || value=
  [ -n "$value" ] || value=unknown
  printf '%s' "$value"
}

write_once() {
  tmp=$(mktemp "$TEXTFILE_DIR/$CHECK.prom.XXXXXX") || {
    echo "$CHECK: cannot create a temp file in $TEXTFILE_DIR" >&2
    return 1
  }
  now=$(date +%s)
  stale=0
  {
    printf '# HELP homelab_iscsi_session_info One series per iSCSI session in sysfs; a state other than LOGGED_IN blocks the next attach of its target on this node.\n'
    printf '# TYPE homelab_iscsi_session_info gauge\n'
    for dir in "$SYSFS"/class/iscsi_session/session*; do
      [ -d "$dir" ] || continue
      session=${dir##*/}
      state=$(read_attr "$dir/state")
      target=$(read_attr "$dir/targetname")
      # A session has one connection, numbered <session>:0.
      connection=$(read_attr "$SYSFS/class/iscsi_connection/connection${session#session}:0/state")
      printf 'homelab_iscsi_session_info{session="%s",state="%s",target="%s",connection_state="%s"} 1\n' \
        "$session" "$state" "$target" "$connection"
      [ "$state" = LOGGED_IN ] || stale=$((stale + 1))
    done
    printf '# HELP homelab_iscsi_sessions_not_logged_in Sessions whose state is not LOGGED_IN.\n'
    printf '# TYPE homelab_iscsi_sessions_not_logged_in gauge\n'
    printf 'homelab_iscsi_sessions_not_logged_in %s\n' "$stale"
    # The homelab_check_* HELP lines must match disk-health.sh's byte for byte. node_exporter
    # drops a family whose HELP differs from an earlier file's and sets
    # node_textfile_scrape_error, so a mismatch loses these series on daniel-box.
    printf "# HELP homelab_check_success 1 when the check's last verdict was up, 0 when down.\n"
    printf '# TYPE homelab_check_success gauge\n'
    printf 'homelab_check_success{check="%s"} %s\n' "$CHECK" "$([ "$stale" -eq 0 ] && echo 1 || echo 0)"
    printf "# HELP homelab_check_last_run_timestamp_seconds Unix time the check's last verdict was taken.\n"
    printf '# TYPE homelab_check_last_run_timestamp_seconds gauge\n'
    printf 'homelab_check_last_run_timestamp_seconds{check="%s"} %s\n' "$CHECK" "$now"
    printf "# HELP homelab_check_deadline_seconds Longest legitimate gap between two of the check's writes.\n"
    printf '# TYPE homelab_check_deadline_seconds gauge\n'
    printf 'homelab_check_deadline_seconds{check="%s"} %s\n' "$CHECK" "$DEADLINE_S"
  } > "$tmp"
  # mktemp creates 0600; the exporter in the other container reads as the same uid, and 0644
  # keeps the contract's mode for any other reader.
  if ! { chmod 0644 "$tmp" && mv "$tmp" "$TEXTFILE_DIR/$CHECK.prom"; }; then
    rm -f "$tmp"
    echo "$CHECK: cannot replace $TEXTFILE_DIR/$CHECK.prom" >&2
    return 1
  fi
}

while :; do
  write_once || true
  [ "$ONCE" = 1 ] && exit 0
  sleep "$INTERVAL_S"
done
