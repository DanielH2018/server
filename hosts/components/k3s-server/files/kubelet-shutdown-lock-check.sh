#!/usr/bin/env bash
# Report whether kubelet holds its logind shutdown delay lock, as a node_exporter textfile verdict.
#
# Usage: kubelet-shutdown-lock-check.sh
# homelab-kubelet-shutdown-lock.timer runs it every 5 minutes on a node whose kubelet runs a
# graceful node shutdown. It writes check `kubelet-shutdown-lock` with success 1 when
# `systemd-inhibit --list` names a `kubelet` lock on `shutdown` in `delay` mode, and 0 otherwise.
# It exits non-zero only when systemd-inhibit cannot list the locks or the textfile write fails.
# Running it by hand is safe and does what the timer does.
#
# WHY. Without the lock, the node's next shutdown stops pods without draining them, and Longhorn's
# ext4 journals can abort. kubelet logs its failure to take the lock once per start and logs
# nothing on success, so only a reading of the lock itself can tell when a fix took effect. The
# KubeletShutdownLock* rules in k8s/roles/observability/files/rules/host-checks.yaml read the file
# this writes.
set -uo pipefail

case "${1:-}" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: kubelet-shutdown-lock-check.sh" >&2; exit 64 ;;
esac

# The packaged unit sets HOMELAB_LIB_DIR to /usr/lib/homelab, where check-lib ships it.
# shellcheck source=/dev/null
source "${HOMELAB_LIB_DIR:-/usr/lib/homelab}/homelab-check-lib.sh" || {
  logger -t kubelet-shutdown-lock "homelab-check-lib.sh missing — install the host package"
  exit 1
}

# Three 5-minute timer runs; move it with homelab-kubelet-shutdown-lock.timer.
DEADLINE_S=900

if ! LOCKS=$(systemd-inhibit --list --no-legend --no-pager 2>&1); then
  echo "systemd-inhibit --list failed: ${LOCKS}"
  write_check_textfile kubelet-shutdown-lock 0 "$(date +%s)" "$DEADLINE_S" \
    || logger -t kubelet-shutdown-lock "textfile write failed"
  exit 1
fi

# WHO can hold spaces, so the match anchors kubelet at the row's start and delay at its end.
if grep -Eq '^kubelet[[:space:]].*[[:space:]]shutdown[[:space:]].*[[:space:]]delay[[:space:]]*$' <<<"$LOCKS"; then
  SUCCESS=1
else
  SUCCESS=0
  echo "kubelet holds no shutdown delay lock; systemd-inhibit lists:"
  echo "${LOCKS:-(no locks)}"
fi

# A missing lock is the verdict, not this unit's failure.
write_check_textfile kubelet-shutdown-lock "$SUCCESS" "$(date +%s)" "$DEADLINE_S" || {
  logger -t kubelet-shutdown-lock "textfile write failed"
  exit 1
}
