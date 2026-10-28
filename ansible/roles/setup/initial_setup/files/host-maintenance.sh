#!/usr/bin/env bash
# Weekly host maintenance -- managed by Ansible (initial_setup role); edits overwritten.
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
# WHY A WRAPPER (#6179). Each cron used to pipe its command straight into `logger`. The
# pipeline's exit status was logger's, and nothing wrote a textfile, so a job that failed every
# week, or stopped running, read the same as one that ran clean. The <job>NotReporting rules in
# ansible/roles/k8s/observability/files/rules/host-checks.yaml read the file this writes.
set -uo pipefail

JOB="${1:-}"
case "$JOB" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  apt-autoremove | dpkg-purge | firmware-update) ;;
  *) echo "usage: host-maintenance.sh apt-autoremove|dpkg-purge|firmware-update" >&2; exit 64 ;;
esac

# shellcheck source=/dev/null
source /usr/local/lib/homelab-check-lib.sh || {
  logger -t "$JOB" "homelab-check-lib.sh missing — run initial_setup on this host"
  exit 1
}

# The longest legitimate gap between two textfile writes: every job runs weekly, 604800s, and
# the extra 2h is slack for a run that takes far longer than the last, as rkhunter-check.sh
# keeps. Move it with the crons. ansible/tests/setup/test_host_maintenance.py pins the rules'
# literals to it.
DEADLINE_S=612000

case "$JOB" in
  apt-autoremove)
    apt-get autoremove -y -q 2>&1
    RC=$?
    SUCCESS=$((RC == 0))
    ;;
  dpkg-purge)
    # `rc` rows are packages removed with their config left behind. pipefail makes a failed
    # purge visible: xargs exits 123 when a dpkg it ran failed.
    dpkg -l | awk '/^rc/{print $2}' | xargs -r dpkg --purge 2>&1
    RC=$?
    SUCCESS=$((RC == 0))
    ;;
  firmware-update)
    # `refresh --force` because a plain refresh exits non-zero once the metadata is younger than
    # its refresh interval, which is the normal weekly case. Its failure is not the verdict:
    # `update` runs against the metadata already on disk either way.
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
