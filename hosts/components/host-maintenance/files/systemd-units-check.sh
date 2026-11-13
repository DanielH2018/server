#!/usr/bin/env bash
# Count this host's failed system units and write the verdict to the node_exporter textfile.
#
# Usage: systemd-units-check.sh
# homelab-systemd-units.timer runs it every 5 minutes on every host. It writes check
# `systemd-units` with success 1 when no system unit is in the failed state, and 0 otherwise,
# plus homelab_systemd_failed_units with the count. It exits non-zero only when systemctl cannot
# list the units or the textfile write fails. Running it by hand is safe and does what the timer
# does.
#
# WHY (#8219). node-exporter runs without --collector.systemd, so no rule reads a unit's state.
# A unit's failure was covered only by its own OnFailure=homelab-alert@ hook or its own textfile
# verdict. A unit with neither failed silently, and one whose hook posted once to Discord stayed
# failed with no alert series behind it. The SystemdUnits* rules in
# ansible/roles/k8s/observability/files/rules/host-checks.yaml read the file this writes.
#
# System units only: a user manager's units belong to that user's session, and a failed one
# says nothing about the host.
set -uo pipefail

case "${1:-}" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: systemd-units-check.sh" >&2; exit 64 ;;
esac

# The packaged unit sets HOMELAB_LIB_DIR to /usr/lib/homelab, where check-lib (pi-heartbeats on
# the Pi) ships it.
# shellcheck source=/dev/null
source "${HOMELAB_LIB_DIR:-/usr/lib/homelab}/homelab-check-lib.sh" || {
  logger -t systemd-units "homelab-check-lib.sh missing — install the host package"
  exit 1
}

# The longest legitimate gap between two textfile writes: three 5-minute timer runs. Move it
# with homelab-systemd-units.timer. hosts/tests/test_systemd_units_check.py pins the rules'
# literals to it.
DEADLINE_S=900

# --plain drops the bullet column, so the unit name is the first field.
if ! FAILED=$(systemctl --system list-units --state=failed --no-legend --plain 2>&1); then
  echo "systemctl list-units failed: ${FAILED}"
  write_check_textfile systemd-units 0 "$(date +%s)" "$DEADLINE_S" \
    || logger -t systemd-units "textfile write failed"
  exit 1
fi
UNITS=$(awk 'NF { print $1 }' <<<"$FAILED")
COUNT=$(grep -c . <<<"$UNITS")
SUCCESS=$((COUNT == 0))

((SUCCESS == 1)) || echo "failed system units (${COUNT}): $(tr '\n' ' ' <<<"$UNITS")"

# A failed unit is the verdict, not this unit's failure: exiting 1 for it would list the checker
# under `systemctl --failed` beside the unit it found.
write_check_textfile systemd-units "$SUCCESS" "$(date +%s)" "$DEADLINE_S" \
  "# HELP homelab_systemd_failed_units System units in the failed state at the check's last run." \
  "# TYPE homelab_systemd_failed_units gauge" \
  "homelab_systemd_failed_units{check=\"systemd-units\"} $COUNT" || {
  logger -t systemd-units "textfile write failed"
  exit 1
}
