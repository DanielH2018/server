#!/usr/bin/env bash
# Shared host-cron state writer — the canonical {ts,ok,msg[,bytes]} state file writer for the crons
# monitor-bridge polls. Sourced (never executed) by container-role host crons that report health via
# a :ro bind-mounted state file. Deployed to /usr/local/lib/state-lib.sh by each consuming role — the
# shell twin of roles/setup/common/files/host_lib.py's cross-role Python sharing (there is no
# ansible/templates macro path for a shell function). Mirrors host_lib.atomic_write: jq (not printf)
# so a stray control char can't make invalid JSON, and temp + atomic rename with the temp chmod'd 0644
# BEFORE the mv — these are root/host crons and the non-root monitor-bridge (UID 1000, :ro) reads STATE
# every 300s with no retry, so a mid-truncate read would page a false "state unparseable" DOWN.
#
# Consumers: wg-easy (pull-pi-peers.sh) and autofix-bridge (autofix-disk-prune.sh) source this
# directly. The traefik role lib (traefik-lib.sh) keeps its own role-local copy of this idiom for
# now — folding it in would only reduce duplication cosmetically at the cost of redeploying the edge
# router, so it's a deliberate deferral. (kopia-lib.sh was the other such copy; the role retired
# 2026-08-13 and now sits at roles/containers/archive/kopia/. BYTES is carried here as a superset,
# originally so those libs COULD delegate without a signature change.)

# state_write STATE TAG OK MSG [BYTES]
# Write {ts, ok, msg} (plus a `bytes` field when BYTES is passed — added for kopia's b2-usage
# gauge, which retired with the role; no current consumer passes it), then log
# to syslog under TAG.
state_write() {
  local state="$1" tag="$2" ok="$3" msg="$4" bytes="${5:-}"
  if [ -n "$bytes" ]; then
    jq -nc --argjson ts "$(date +%s)" --argjson ok "$ok" --argjson bytes "$bytes" --arg msg "$msg" \
      '{ts: $ts, ok: $ok, bytes: $bytes, msg: $msg}' >"$state.tmp"
  else
    jq -nc --argjson ts "$(date +%s)" --argjson ok "$ok" --arg msg "$msg" \
      '{ts: $ts, ok: $ok, msg: $msg}' >"$state.tmp"
  fi && chmod 0644 "$state.tmp" && mv -f "$state.tmp" "$state"
  logger -t "$tag" "$ok: $msg"
}
