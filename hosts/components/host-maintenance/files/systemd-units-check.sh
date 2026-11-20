#!/usr/bin/env bash
# Count this host's failed system units and write the verdict to the node_exporter textfile.
#
# Usage: systemd-units-check.sh
# homelab-systemd-units.timer runs it every 5 minutes on every host. It writes check
# `systemd-units` with success 1 when no system unit is in the failed state, and 0 otherwise,
# plus homelab_systemd_failed_units with the count and one homelab_systemd_unit_failed series
# per failed unit, labelled with the unit's name. A unit whose OnFailure= Discord hook has paged
# since the unit failed also carries paged_by="homelab-alert". The hook is a homelab-alert@
# instance or a unit's own homelab-<name>-alert.service, and a page its rate limit skipped counts.
# The run that first finds a failure decides the label. It records the decision in
# $STATE_DIRECTORY/paged-by, keyed by the boot ID and the unit's InactiveEnterTimestampMonotonic,
# so later runs copy it for that same failure and decide again for a new one.
# It exits non-zero only when systemctl cannot list the units or the textfile write fails.
# Running it by hand is safe and does what the timer does.
#
# WHY. node-exporter runs without --collector.systemd, so no rule reads a unit's state, and a
# unit with no OnFailure= hook and no textfile verdict of its own would fail silently. The
# SystemdUnits* rules in k8s/roles/observability/files/rules/host-checks.yaml read the file this
# writes. Alertmanager sends a paged_by alert to its blackhole, so a unit whose hook delivered
# pages once. A hook that is still retrying, or that never ran, when the failure is first found
# leaves the label off for the whole failure, and Alertmanager pages for the unit. The label
# never changes mid-failure, because a changed label set resolves the firing alert, and
# Discord would receive a RESOLVED notice for a unit that is still failed. A unit that fails
# again, or fails after a reboot, is a new failure and gets a new decision.
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

# Prints the value of property $1 from the `systemctl show` output on stdin.
prop() { awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }'; }

# Succeeds when the `systemctl show -p OnFailure -p InactiveEnterTimestampMonotonic` output in $1
# names a Discord hook, homelab-alert@ or homelab-<name>-alert.service, that has paged since the
# unit failed: inactive with Result=success or Result=exec-condition, and inactive since a time
# after the unit's own failure. A hook still retrying, or one that never ran, fails this.
hook_delivered() {
  local hook failed_at props delivered_at
  hook=$(prop OnFailure <<<"$1" | tr ' ' '\n' | grep -m1 -E '^homelab-(alert@|[a-z0-9-]+-alert\.service$)') || return 1
  failed_at=$(prop InactiveEnterTimestampMonotonic <<<"$1")
  props=$(systemctl show -p ActiveState -p Result -p InactiveEnterTimestampMonotonic "$hook") || return 1
  delivered_at=$(prop InactiveEnterTimestampMonotonic <<<"$props")
  # DECIDED: a page the hook's ExecCondition= rate limit skipped mutes SystemdUnitsFailed like a
  # delivered one. The skip proves a delivery inside the window, and a unit that re-fails each
  # tick is a new failure every run, so counting a skip as unpaged would flip the label and page
  # twice. A unit that recovers and fails again inside the window pages only via WarningUnresolved.
  [[ $(prop ActiveState <<<"$props") == inactive && $(prop Result <<<"$props") =~ ^(success|exec-condition)$ ]] &&
    ((${delivered_at:-0} > ${failed_at:-0}))
}

# One line per failed unit: "<boot ID>:<InactiveEnterTimestampMonotonic> <paged|unpaged> <unit>".
# The packaged unit's StateDirectory= sets STATE_DIRECTORY.
STATE_FILE="${STATE_DIRECTORY:-/var/lib/homelab-systemd-units}/paged-by"
PREVIOUS_STATE=$(cat "$STATE_FILE" 2>/dev/null)
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
STATE=()

# One series per failed unit, so SystemdUnitsFailed can name each unit in its summary. Unit
# names carry systemd's \x2d escapes, and node_exporter rejects the whole file on a label value
# with a backslash or quote the text format does not escape.
PER_UNIT=()
if ((COUNT > 0)); then
  PER_UNIT+=(
    "# HELP homelab_systemd_unit_failed 1 for each system unit in the failed state at the check's last run."
    "# TYPE homelab_systemd_unit_failed gauge"
  )
  while IFS= read -r unit; do
    props=$(systemctl show -p OnFailure -p InactiveEnterTimestampMonotonic "$unit")
    failure="${BOOT_ID}:$(prop InactiveEnterTimestampMonotonic <<<"$props")"
    decision=$(F="$failure" U="$unit" awk '$1 == ENVIRON["F"] && $3 == ENVIRON["U"] { print $2; exit }' <<<"$PREVIOUS_STATE")
    if [[ -z $decision ]]; then
      decision=unpaged
      hook_delivered "$props" && decision=paged
    fi
    STATE+=("$failure $decision $unit")
    escaped=${unit//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    paged_by=""
    [[ $decision == paged ]] && paged_by=',paged_by="homelab-alert"'
    PER_UNIT+=("homelab_systemd_unit_failed{check=\"systemd-units\",unit=\"${escaped}\"${paged_by}} 1")
  done <<<"$UNITS"
fi

# Without the state file every run decides afresh, as if each failure were new.
if ! {
  tmp=$(mktemp "${STATE_FILE}.XXXXXX" 2>/dev/null) &&
    { ((${#STATE[@]} == 0)) || printf '%s\n' "${STATE[@]}"; } >"$tmp" &&
    mv "$tmp" "$STATE_FILE"
}; then
  logger -t systemd-units "cannot write ${STATE_FILE}"
fi

# A failed unit is the verdict, not this unit's failure: exiting 1 for it would list the checker
# under `systemctl --failed` beside the unit it found.
write_check_textfile systemd-units "$SUCCESS" "$(date +%s)" "$DEADLINE_S" \
  "# HELP homelab_systemd_failed_units System units in the failed state at the check's last run." \
  "# TYPE homelab_systemd_failed_units gauge" \
  "homelab_systemd_failed_units{check=\"systemd-units\"} $COUNT" \
  "${PER_UNIT[@]}" || {
  logger -t systemd-units "textfile write failed"
  exit 1
}
