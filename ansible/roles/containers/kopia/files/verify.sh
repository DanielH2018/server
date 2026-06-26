#!/usr/bin/env bash
# Weekly backup integrity VERIFY — managed by Ansible (kopia role); edits overwritten.
#
# The VERIFY tier of the three-tier backup assurance (snapshot freshness -> weekly verify
# -> monthly restore drill). `kopia snapshot verify --verify-files-percent=1` re-reads
# every snapshot's metadata/index from B2 and downloads a 1% sample of file contents,
# checking their hashes — proving stored blobs are READABLE across ALL snapshots (the
# restore drill, by contrast, proves ONE service's tree actually restores). It runs
# against the already-connected repo inside the running container (reusing its B2
# connection + KOPIA_PASSWORD), is read-only, and is safe alongside the live server.
#
# Reporting: writes {"ts","ok","msg"} to STATE; monitor-bridge's `verify` check reads it
# (read-only bind mount) and pushes the "Backup Verify" Kuma monitor every cycle — a
# FAILED verify (detected bit-rot / an unreadable blob), staleness, or a missing state
# file all alert. This REPLACES the old `... 2>&1 | logger` cron, whose pipe made cron
# see logger's (always-0) exit code, silently swallowing a non-zero verify.
set -uo pipefail

STATE=/var/lib/kopia-verify/state.json

write_state() { # ok msg
  printf '{"ts": %s, "ok": %s, "msg": "%s"}\n' "$(date +%s)" "$1" "$2" > "$STATE"
  logger -t kopia-verify "$1: $2"
}

# Capture output + the verify's OWN exit code (the command substitution's status is
# `docker exec`'s, which propagates kopia's — no `| logger` to mask it this time).
OUT=$(docker exec kopia kopia snapshot verify --verify-files-percent=1 2>&1)
RC=$?

# Collapse to a single-line summary for the Kuma msg / state file: prefer kopia's final
# "Finished verifying N snapshots..." / error line, else fall back to the last line.
# Strip quotes/newlines so the value stays valid inside the JSON string.
SUMMARY=$(printf '%s' "$OUT" | grep -iE 'verif|error|fail' | tail -1 | tr -d '"' | tr '\n' ' ')
[ -n "$SUMMARY" ] || SUMMARY=$(printf '%s' "$OUT" | tail -1 | tr -d '"' | tr '\n' ' ')

if [ "$RC" -eq 0 ]; then
  write_state true "${SUMMARY:-verify ok}"
else
  write_state false "verify exit $RC: ${SUMMARY:-see kopia-verify syslog}"
  exit 1
fi
