#!/usr/bin/env bash
# Weekly host maintenance -- shipped by the host package; edits are overwritten.
#
# Usage: host-maintenance.sh JOB
# JOB is one of:
#   apt-autoremove   apt-get autoremove, cron "Weekly apt autoremove" (Sunday 02:00, every host)
#   dpkg-purge       purge the config remnants of removed packages, cron "Weekly dpkg purge
#                    orphaned configs" (Sunday 02:15, every host)
#   firmware-update  fwupdmgr refresh and update, cron "Weekly firmware update" (Sunday 07:00,
#                    hosts with fwupd)
# It prints the job's output on stdout, where the cron pipes it to `logger` under the job's old
# tag, and writes the verdict to the node_exporter textfile as check JOB. Running it by hand is
# safe and does what the cron does.
#
# WHY A WRAPPER. A cron that pipes its command straight into `logger` reports logger's exit
# status and writes no textfile, so a job that failed every week, or stopped running, reads the
# same as one that ran clean. The <job>NotReporting rules in
# k8s/roles/observability/files/rules/host-checks.yaml read the file this writes.
set -uo pipefail

JOB="${1:-}"
case "$JOB" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  apt-autoremove | dpkg-purge | firmware-update) ;;
  *) echo "usage: host-maintenance.sh apt-autoremove|dpkg-purge|firmware-update" >&2; exit 64 ;;
esac

# The packaged units set HOMELAB_LIB_DIR to /usr/lib/homelab, where the check-lib component ships it.
# shellcheck source=/dev/null
source "${HOMELAB_LIB_DIR:-/usr/lib/homelab}/homelab-check-lib.sh" || {
  logger -t "$JOB" "homelab-check-lib.sh missing — install the host package"
  exit 1
}

# Weekly jobs (604800s) plus 2h slack for a run longer than the last; move it with the crons.
DEADLINE_S=612000

case "$JOB" in
  apt-autoremove)
    apt-get autoremove -y -q 2>&1
    RC=$?
    SUCCESS=$((RC == 0))
    ;;
  dpkg-purge)
    # `rc` rows: removed packages with config left; pipefail surfaces xargs exit 123 (a dpkg
    # failed).
    dpkg -l | awk '/^rc/{print $2}' | xargs -r dpkg --purge 2>&1
    RC=$?
    SUCCESS=$((RC == 0))
    ;;
  firmware-update)
    # `refresh --force`: a plain refresh exits non-zero when metadata is fresh, the normal weekly
    # case.
    # Its failure is not the verdict; `update` uses the metadata on disk.
    fwupdmgr refresh --force >/dev/null 2>&1
    # `--no-reboot-check` because the weekly restart 30 minutes later commits the update.
    fwupdmgr update -y --no-reboot-check 2>&1
    RC=$?
    # fwupdmgr exits 2 when there is nothing to update, the normal weekly case.
    SUCCESS=$((RC == 0 || RC == 2))
    ;;
esac

write_check_textfile "$JOB" "$SUCCESS" "$(date +%s)" "$DEADLINE_S" \
  || logger -t "$JOB" "textfile write failed (exit ${RC})"

# Cron ignores the exit code; it is non-zero after a failure so a run by hand reads the same way.
((SUCCESS == 1)) || {
  echo "${JOB} failed: exit ${RC}"
  exit 1
}
