#!/bin/bash
# Pull daniel-pi's wg-easy peer configs (wg0.conf/wg0.json — WireGuard private keys)
# into the Longhorn-backed /data PVC, so an SD-card death on the Pi doesn't mean
# re-enrolling every VPN client. Successor to the daniel-server host cron (host flip 1,
# 2026-08-14): the Longhorn 03:30 nightly carries the PVC to B2, replacing the retired
# Kopia scope.
#
# sudo rsync on the Pi (NOPASSWD sudo for the ubuntu user) is REQUIRED: the files are
# root:root 0600/0640. StrictHostKeyChecking=yes against the deploy-pinned known_hosts
# (stricter than the old accept-new — the pin is mounted, never TOFU'd here). No
# --delete: a removed peer lingering in the backup is harmless, and --delete would wipe
# the copy if the source were ever momentarily empty.
#
# Reporting: pushes the "WG Pi Peer Backup" Kuma monitor DIRECTLY (up on success, down
# with the error otherwise) — the monitor-bridge `pi_peers` state-file check this
# replaces retired with the flip. It also pings an off-premises Healthchecks.io check when
# HC_PING_URL is set, which is what still alerts when the whole cluster (Kuma included) is
# down. `set -uo` (not -euo) so the rsync exit code is captured and reported, not swallowed.
set -uo pipefail

: "${PI_SRC:?}" "${KUMA_PUSH_URL:?}"
# A subdir, not the PVC mountpoint: /data itself is root-owned (fsGroup grants group
# write, not ownership), so rsync -a's final set-times on the dest dir EPERMs (exit 23).
# A subdir we create is ours and takes attrs cleanly.
DEST=/data/peers

# The Secret volume is root-owned 0440 (group-read via fsGroup); ssh refuses a
# group-readable identity file, so stage a private 0400 copy we own.
install -m 0700 -d "$HOME/.ssh"
install -m 0400 /ssh/id "$HOME/.ssh/id"

# k8s/cronjob-gate (ansible/roles/k8s/cronjob-gate) runs a one-off gate Job named
# pi-peer-backup-deploy-gate on every deploy of this role, to prove a bumped image still runs.
# That run is not the scheduled backup, and it must not report as one: pushing here would make
# the Kuma monitor mean "something ran" instead of "the nightly backup ran", masking a missed
# 23:30 firing for the rest of its 2.5-day window, and a failing gate run would page the
# operator from a deploy-time probe rather than from the backup itself. The pod's hostname is
# `<job-name>-<random>` (kubectl create job --from=cronjob copies the pod spec verbatim, so
# there is no other way for the script to tell the two runs apart).
GATE_RUN=0
[[ "${HOSTNAME:-}" == pi-peer-backup-deploy-gate-* ]] && GATE_RUN=1

push() { # status msg
  if [[ "$GATE_RUN" -eq 1 ]]; then
    echo "deploy-gate run — not pushing Kuma/Healthchecks ($1: $2)"
    return
  fi

  curl -fsS -m 10 --get "$KUMA_PUSH_URL" \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" >/dev/null \
    || echo "kuma push failed ($1: $2)" >&2

  # Kuma resolves to a Service in this cluster, so a cluster outage silences the push above and
  # the monitor waiting for it — nothing alerts. hc-ping.com is off-site and alerts on the
  # silence instead. Empty when no ping key is configured, in which case this is a no-op.
  if [[ -n "${HC_PING_URL:-}" ]]; then
    local url="$HC_PING_URL"
    [[ "$1" == "up" ]] || url="$url/fail"
    # Deliberately not "$2": an rsync failure echoes PI_SRC, which carries the Pi's LAN IP and
    # ssh user, and Healthchecks.io stores ping bodies. The status is what has to escape the
    # house; the detail stays in Kuma, which is on the LAN.
    curl -fsS -m 10 --retry 3 --data-raw "peer pull reported $1; detail in Kuma" "$url" >/dev/null \
      || echo "healthchecks ping failed ($1: $2)" >&2
  fi
}

# --timeout bounds a connected-but-stalled transfer, which --connect-timeout does not cover: a
# Pi that accepts the connection and then hangs would otherwise sit until the CronJob's own
# activeDeadlineSeconds (600s) kills the pod, at which point the controller deletes it, the
# deploy-time gate reads no container state, and the deploy fails on a misleading message
# instead of this script's own. 120s is comfortably below that deadline for two small files.
OUT=$(rsync -a --chmod=D700 --timeout=120 \
  --rsync-path='sudo rsync' \
  -e "ssh -i $HOME/.ssh/id -o UserKnownHostsFile=/ssh/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=15" \
  "$PI_SRC" "$DEST/" 2>&1)
RC=$?

if [ "$RC" -ne 0 ]; then
  SUMMARY=$(printf '%s' "$OUT" | tail -1 | tr -d '"' | tr '\n' ' ')
  push down "rsync exit $RC: ${SUMMARY:-unknown}"
  exit 1
fi

# File-count floor: a momentarily empty/unreadable source can rsync clean and, with no
# --delete, leave a stale-but-present copy. The Pi's config always holds at least
# wg0.conf + wg0.json.
FILES=$(find "$DEST" -type f | wc -l | tr -d '[:space:]')
if [ "${FILES:-0}" -lt 2 ]; then
  push down "pulled but only ${FILES:-0} file(s) in /data (expected >=2: wg0.conf + wg0.json)"
  exit 1
fi

push up "pulled $FILES peer file(s) from daniel-pi"
