#!/usr/bin/env bash
# Shared helpers for the traefik host crons (docker-user-verify / appsec-verify / cloudflare-ip-drift
# / crowdsec-update-home-allowlist) — managed by Ansible (traefik role); edits overwritten. Sourced
# from /usr/local/lib/traefik-lib.sh; not executed directly. Mirrors kopia-lib.sh (kopia role).

# traefik_write_state STATE TAG OK MSG
# Atomically write the {ts, ok, msg} state file monitor-bridge polls, then log to syslog. jq (not
# printf) so a stray control char can't make hand-built JSON invalid. Temp + atomic rename, chmod'ing
# the temp 0644 BEFORE the mv: these are ROOT crons and the non-root monitor-bridge (UID 1000, :ro
# bind) reads STATE every 300s with no retry — a mid-truncate read would page a false "state
# unparseable" DOWN, and the swapped-in file must be world-readable the instant it appears (a plain
# `>` truncates in place, exposing the half-written file and preserving root's 0027 umask mode).
traefik_write_state() {
  local state="$1" tag="$2" ok="$3" msg="$4"
  jq -nc --argjson ts "$(date +%s)" --argjson ok "$ok" --arg msg "$msg" \
    '{ts: $ts, ok: $ok, msg: $msg}' >"$state.tmp" \
    && chmod 0644 "$state.tmp" && mv -f "$state.tmp" "$state"
  logger -t "$tag" "$ok: $msg"
}
