#!/bin/bash
# Pull daniel-pi's wg-easy peer configs (wg0.conf/wg0.json — WireGuard private keys)
# into the Longhorn-backed /data PVC, so an SD-card death on the Pi doesn't mean
# re-enrolling every VPN client. The Longhorn 03:30 nightly carries the PVC to B2.
#
# sudo rsync on the Pi (NOPASSWD sudo for the ubuntu user) is REQUIRED: the files are
# root:root 0600/0640. StrictHostKeyChecking=yes against the known_hosts pin committed in
# the role (the pin is mounted, never TOFU'd here). No
# --delete: a removed peer lingering in the backup is harmless, and --delete would wipe
# the copy if the source were ever momentarily empty.
#
# Reporting: the verdict leaves the pod as annotations on one ConfigMap (VERDICT_CONFIGMAP),
# which kube-state-metrics exports to Prometheus. The image carries no kubectl, so export_verdict sends one
# merge PATCH with curl and the pod's ServiceAccount token. A failed export makes the run exit
# 1, so the CronJob's last successful time stops moving and the NotReporting rule fires. The
# run also pings an off-premises Healthchecks.io check when HC_PING_URL is set, which is what
# still alerts when the whole cluster is down. `set -uo` (not -euo) so the rsync exit code is
# captured and reported, not swallowed.
#
# Usage: pull-pi-peers.sh (no arguments)
# Runs as the pi-peer-backup CronJob at 23:00 America/Chicago, and once per deploy as the
# pi-peer-backup-deploy-gate Job. Environment: PI_SRC (required, the Pi's source
# directory), HC_PING_URL (optional Healthchecks.io ping),
# VERDICT_CONFIGMAP (optional; the ConfigMap the verdict is written to, empty for none),
# and HOSTNAME (a pod name starting pi-peer-backup-deploy-gate- skips the ping and the
# export). Reads the SSH identity at /ssh/id and writes to /data/peers.
set -uo pipefail

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

: "${PI_SRC:?}"
# Subdir, not /data: /data is root-owned, so rsync -a's final set-times on it EPERMs (exit 23).
DEST=/data/peers

# ssh refuses a group-readable identity file, so stage a private 0400 copy we own.
install -m 0700 -d "$HOME/.ssh"
install -m 0400 /ssh/id "$HOME/.ssh/id"

# The deploy-gate Job (hostname pi-peer-backup-deploy-gate-*) runs this script; it must not ping or
# export, or the check would mean "something ran" and mask a missed 23:00 firing.
GATE_RUN=0
[[ "${HOSTNAME:-}" == pi-peer-backup-deploy-gate-* ]] && GATE_RUN=1

# Syslog shape because the pod has no `logger`; busybox `date` has no `-Is`.
report() { # line
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${HOSTNAME:-pi-peer-backup} pi-peer-backup: $1"
}

# enableServiceLinks is false, so reach the API by DNS name; KUBERNETES_SERVICE_HOST is not set.
SA_DIR="${SERVICEACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
VERDICT_CONFIGMAP="${VERDICT_CONFIGMAP:-}"
EXPORT_FAILED=0

# Bare PATCH of the verdict annotations: the Role grants `patch` on that name only, with no GET.
# IPv4 addresses are redacted from the message; sets EXPORT_FAILED on failure, no-op for the gate.
export_verdict() { # status msg
  [[ -n "${VERDICT_CONFIGMAP:-}" && "$GATE_RUN" -eq 0 ]] || return 0
  local msg body code rc ns
  msg=$(printf '%s' "$2" | sed -E 's/[0-9]{1,3}(\.[0-9]{1,3}){3}/<ip>/g' | tr -d '"\134' | tr '\n\t' '  ' | cut -c1-200)
  body=$(printf '{"metadata":{"annotations":{"homelab/check-verdict":"%s","homelab/check-last-run":"%s","homelab/check-message":"%s"}}}' \
    "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$msg")
  ns=$(cat "$SA_DIR/namespace" 2>/dev/null) || ns=""
  code=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X PATCH \
    --cacert "$SA_DIR/ca.crt" \
    -H "Authorization: Bearer $(cat "$SA_DIR/token" 2>/dev/null)" \
    -H 'Content-Type: application/merge-patch+json' \
    --data "$body" \
    "https://kubernetes.default.svc/api/v1/namespaces/${ns}/configmaps/${VERDICT_CONFIGMAP}" 2>/dev/null) && rc=0 || rc=$?
  if [[ "$rc" -ne 0 || "$code" != 200 ]]; then
    report "export failed: cannot patch ${VERDICT_CONFIGMAP} (http=${code:-none} rc=${rc})" >&2
    EXPORT_FAILED=1
  fi
}

# Reports one run: the verdict line, the ConfigMap export and the Healthchecks.io ping.
push() { # status msg
  if [[ "$GATE_RUN" -eq 1 ]]; then
    echo "deploy-gate run — not reporting ($1: $2)"
    return
  fi

  report "status=$1 $2"
  export_verdict "$1" "$2"

  # hc-ping.com is off-site and alerts on silence: a cluster outage stops every in-cluster alert.
  if [[ -n "${HC_PING_URL:-}" ]]; then
    local url="$HC_PING_URL"
    [[ "$1" == "up" ]] || url="$url/fail"
    # Deliberately not "$2": rsync errors echo PI_SRC (LAN IP, ssh user) and Healthchecks.io stores
    # ping bodies. No `-S`, stderr discarded, as in longhorn-backup-health.sh.j2.
    curl -fs -m 10 --retry 3 --data-raw "peer pull reported $1; detail in the pod log" "$url" >/dev/null 2>&1 \
      || echo "healthchecks ping failed ($1: $2)" >&2
  fi
}

# --timeout bounds a stalled transfer that --connect-timeout misses; keep it under the CronJob's
# 600s activeDeadlineSeconds, whose pod kill makes the deploy gate fail on a misleading message.
OUT=$(rsync -a --chmod=D700 --timeout=120 \
  --rsync-path='sudo rsync' \
  -e "ssh -i $HOME/.ssh/id -o UserKnownHostsFile=/ssh/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=15" \
  "$PI_SRC" "$DEST/" 2>&1)
RC=$?

# ssh exits 255 for any connection failure, so only its own line tells a host-key change apart.
if [ "$RC" -eq 255 ] && [[ "$OUT" == *"Host key verification failed."* ]]; then
  push down "daniel-pi host key differs from the committed pin"
  exit 1
fi

if [ "$RC" -ne 0 ]; then
  SUMMARY=$(printf '%s' "$OUT" | tail -1 | tr -d '"' | tr '\n' ' ')
  push down "rsync exit $RC: ${SUMMARY:-unknown}"
  exit 1
fi

# Floor: an empty source can rsync clean, with no --delete, leaving a stale copy; expect >=2 files.
FILES=$(find "$DEST" -type f | wc -l | tr -d '[:space:]')
if [ "${FILES:-0}" -lt 2 ]; then
  push down "pulled but only ${FILES:-0} file(s) in /data (expected >=2: wg0.conf + wg0.json)"
  exit 1
fi

push up "pulled $FILES peer file(s) from daniel-pi"
# A failed verdict export fails the Job, so PiPeerBackupNotReporting fires.
[[ "$EXPORT_FAILED" -eq 0 ]] || exit 1
