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

push() { # status msg
  curl -fsS -m 10 --get "$KUMA_PUSH_URL" \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" >/dev/null \
    || echo "kuma push failed ($1: $2)" >&2

  # Kuma resolves to a Service in this cluster, so a cluster outage silences the push above and
  # the monitor waiting for it — nothing alerts. hc-ping.com is off-site and alerts on the
  # silence instead. Empty when no ping key is configured, in which case this is a no-op.
  if [[ -n "${HC_PING_URL:-}" ]]; then
    local url="$HC_PING_URL"
    [[ "$1" == "up" ]] || url="$url/fail"
    curl -fsS -m 10 --retry 3 --data-raw "$2" "$url" >/dev/null \
      || echo "healthchecks ping failed ($1: $2)" >&2
  fi
}

OUT=$(rsync -a --chmod=D700 \
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
