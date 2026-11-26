#!/usr/bin/env bash
# Count the *.ucf-dist and *.dpkg-dist copies under /etc that have waited more than a day, and
# write the verdict to the node_exporter textfile.
#
# Usage: ucf-dist-check.sh
# homelab-ucf-dist.timer runs it every hour on every host. It writes check `ucf-dist` with
# success 1 when no such copy under /etc is older than GRACE_S, and 0 otherwise, plus
# homelab_ucf_dist_pending_files with the count of those older copies. It prints each one's
# path, so `journalctl -t ucf-dist` names them. It exits non-zero only when find cannot read
# /etc or the textfile write fails. Running it by hand is safe and does what the timer does.
#
# WHY. When an upgrade brings a new version of a ucf-managed config file that carries a local
# edit, ucf keeps the installed file and leaves the new one beside it as *.ucf-dist. dpkg does
# the same for a conffile, as *.dpkg-dist, under the --force-confold every host sets. A program
# whose packaged snippets need the new version then fails with nothing naming the cause, as
# AIDE 0.19 did while its aide.conf stayed at 0.18. The UcfDist* rules in
# k8s/roles/observability/files/rules/host-checks.yaml read the file this writes.
#
# The age is the file's ctime. ucf and dpkg keep the package's mtime on the dist copy, so the
# mtime can predate the upgrade by a year.
set -uo pipefail

case "${1:-}" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: ucf-dist-check.sh" >&2; exit 64 ;;
esac

# The packaged unit sets HOMELAB_LIB_DIR to /usr/lib/homelab, where check-lib (pi-heartbeats on
# the Pi) ships it.
# shellcheck source=/dev/null
source "${HOMELAB_LIB_DIR:-/usr/lib/homelab}/homelab-check-lib.sh" || {
  logger -t ucf-dist "homelab-check-lib.sh missing — install the host package"
  exit 1
}

# Three hourly timer runs; move it with homelab-ucf-dist.timer.
DEADLINE_S=10800
# A copy younger than this is an upgrade still in progress, or one the operator is merging.
GRACE_S=86400
# 2026-11-22 00:00 UTC. The *.dpkg-dist copies the 26.04 upgrade left before it do not count.
# TODO: https://github.com/DanielH2018/server/issues/10123 - remove once they are reviewed.
DPKG_SINCE=1795305600

ETC_DIR=${ETC_DIR:-/etc}
NOW=$(date +%s)

# DECIDED: a conffile the repo edits gets no exemption. Its copy is how a host shows that the
# package stopped reading the edited setting; dpkg writes one only when the packaged file changes.
# DECIDED: *.dpkg-old is not counted. ca-certificates rewrites its own on every bundle change, and
# dpkg writes none under --force-confold.
if ! FOUND=$(find "$ETC_DIR" -xdev \( -name '*.ucf-dist' -o -name '*.dpkg-dist' \) -printf '%C@ %p\n' 2>&1); then
  echo "find ${ETC_DIR} failed: ${FOUND}"
  write_check_textfile ucf-dist 0 "$NOW" "$DEADLINE_S" ||
    logger -t ucf-dist "textfile write failed"
  exit 1
fi
OVERDUE=$(awk -v cutoff=$((NOW - GRACE_S)) -v since="$DPKG_SINCE" \
  '/\.dpkg-dist$/ && int($1) < since { next }
   NF && int($1) < cutoff { sub(/^[^ ]+ /, ""); print }' <<<"$FOUND" | sort)
COUNT=$(grep -c . <<<"$OVERDUE")
SUCCESS=$((COUNT == 0))

((SUCCESS == 1)) || echo "pending package config copies older than ${GRACE_S}s (${COUNT}): $(tr '\n' ' ' <<<"$OVERDUE")"

# A pending copy is the verdict: exiting 1 would make SystemdUnitsFailed page the same cause twice.
write_check_textfile ucf-dist "$SUCCESS" "$NOW" "$DEADLINE_S" \
  "# HELP homelab_ucf_dist_pending_files *.ucf-dist and *.dpkg-dist copies under /etc older than a day at the check's last run." \
  "# TYPE homelab_ucf_dist_pending_files gauge" \
  "homelab_ucf_dist_pending_files{check=\"ucf-dist\"} $COUNT" || {
  logger -t ucf-dist "textfile write failed"
  exit 1
}
