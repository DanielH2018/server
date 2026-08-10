#!/usr/bin/env bash
# Shared helpers for the kopia host crons (verify / content-verify / maintenance-check /
# restore-drill / b2-usage) — managed by Ansible (kopia role); edits overwritten.
# Sourced from /usr/local/lib/kopia-lib.sh; not executed directly.

# kopia_write_state STATE TAG OK MSG [BYTES]
# Atomically write the {ts, ok, msg} state file monitor-bridge polls (plus a `bytes` field when
# BYTES is passed — the b2-usage billable-bytes gauge), then log to syslog. jq (not printf) so a
# stray backslash/control char in the kopia-derived MSG can't make invalid JSON. Temp + atomic
# rename: monitor-bridge reads STATE every 300s with no retry, so a read landing mid-truncate would
# see a half-written file and page a false "state unparseable" DOWN.
kopia_write_state() {
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

# kopia_summarize OUT
# Collapse a kopia command's output to a single-line summary for the Kuma msg / state file: prefer
# the last line matching verif/error/fail (kopia's "Finished verifying N snapshots..." or its error
# line), else fall back to the very last line. Strips quotes/newlines so the value stays valid inside
# the JSON string kopia_write_state builds.
kopia_summarize() {
  local out="$1" summary
  summary=$(printf '%s' "$out" | grep -iE 'verif|error|fail' | tail -1 | tr -d '"' | tr '\n' ' ')
  [ -n "$summary" ] || summary=$(printf '%s' "$out" | tail -1 | tr -d '"' | tr '\n' ' ')
  printf '%s' "$summary"
}
