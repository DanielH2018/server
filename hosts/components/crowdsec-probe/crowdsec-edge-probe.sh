#!/usr/bin/env bash
# Probe the edge VIP from daniel-pi once and append the HTTP status to the probe's status file.
# Shipped by homelab-daniel-pi (hosts/components/crowdsec-probe); edits are overwritten.
#
# The evidence half of crowdsec's off-cluster verify (D5, *crowdsec's verify from outside the
# cluster*, and its Open question 9, confirmed by the operator on 2026-10-18). A ban is enforced
# against the connecting address, so only a host the cluster bans can prove the edge enforces
# it. The `crowdsec-verify` Job bans this Pi for a bounded window and reads the 403s this probe
# records. The Pi's Alloy counts each line by status (`stage.metrics` in
# roles/containers/alloy/templates/config.alloy.j2), and Prometheus scrapes that counter
# directly as `alloy-pi`. That path never crosses Traefik, which refuses the Pi for the whole
# ban.
#
# The host name must match a real router: a request matching none 404s without consulting the
# bouncer, and `--resolve` keeps the SNI and the Host header equal. jellyfin's hostname matches
# from any client address, as tasks/verify.yml in roles/k8s/crowdsec already relies on.
#
# The file is `*.status`, not `*.log`, so the Pi's Alloy does not ship it to Loki as a syslog
# stream: one line a minute is evidence for a counter, not a log. Its lines carry no `status=`
# token either way, because `probe.py alerts` reads that token as a cron verdict.
#
# Bounded on log2ram's tmpfs by rename, never by truncation. Alloy tracks a byte offset in the
# file, and a file truncated and rewritten in place would make it re-read and re-count old
# lines, which could count a stale 403 inside a verify window. A renamed file is a new inode,
# which Alloy follows from its start. At ~40 B a line, 256 KiB is about four and a half days.
#
# Usage: crowdsec-edge-probe <hostname> <vip>
# Runs as homelab-crowdsec-probe.service, fired by homelab-crowdsec-probe.timer every minute. It
# appends `<UTC time> http=<code>` to /var/log/pi-health/crowdsec-edge-probe.status, with
# `http=000` when no answer came back. It exits 0 whatever the edge answered.
set -uo pipefail

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

if [[ "$#" -ne 2 ]]; then
  echo "usage: crowdsec-edge-probe <hostname> <vip>" >&2
  exit 2
fi

STATUS_FILE=${CROWDSEC_EDGE_PROBE_FILE:-/var/log/pi-health/crowdsec-edge-probe.status}
MAX_BYTES=262144

code=$(curl -sk -o /dev/null --max-time 10 -w '%{http_code}' \
  --resolve "$1:443:$2" "https://$1/") || true
[[ -n "$code" ]] || code=000

if [[ -f "$STATUS_FILE" && "$(stat -c %s "$STATUS_FILE")" -ge "$MAX_BYTES" ]]; then
  mv -f "$STATUS_FILE" "$STATUS_FILE.1"
fi
printf '%s http=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$code" >>"$STATUS_FILE"
