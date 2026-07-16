#!/usr/bin/env bash
# Quarterly DEEP content VERIFY — managed by Ansible (kopia role); edits overwritten.
#
# Belt-and-suspenders on the weekly 1% verify.sh: `kopia snapshot verify --verify-files-percent=25`
# re-reads a QUARTER of every snapshot's file CONTENT from B2 and checks hashes, so a mis-purge or
# silent bit-rot the 1% sample keeps missing gets caught within a quarter. (B2 already read-verifies
# its own SHA1, so this is defense in depth — the extra assurance is against a kopia-side index/blob
# accounting bug a structural verify wouldn't read deeply enough to hit.) Quarterly, not weekly:
# 25% is 25x the download of the weekly pass. Read-only, reuses the running container's B2
# connection + KOPIA_PASSWORD, safe alongside the live server.
#
# Reporting: writes {"ts","ok","msg"} to STATE; monitor-bridge's `content_verify` check reads it
# (read-only bind mount) and pushes the "Backup Content Verify" Kuma monitor — a FAILED verify,
# staleness (>1 quarter + margin = the cron broke), or a missing/corrupt state file all alert.
set -uo pipefail

# shellcheck source=/dev/null
source /usr/local/lib/kopia-lib.sh
STATE=/var/lib/kopia-content-verify/state.json
write_state() { kopia_write_state "$STATE" kopia-content-verify "$@"; } # ok msg

# Capture output + the verify's OWN exit code (command substitution status is docker exec's, which
# propagates kopia's — no `| logger` to mask it).
OUT=$(docker exec kopia kopia snapshot verify --verify-files-percent=25 2>&1)
RC=$?

SUMMARY=$(printf '%s' "$OUT" | grep -iE 'verif|error|fail' | tail -1 | tr -d '"' | tr '\n' ' ')
[ -n "$SUMMARY" ] || SUMMARY=$(printf '%s' "$OUT" | tail -1 | tr -d '"' | tr '\n' ' ')

if [ "$RC" -eq 0 ]; then
  write_state true "${SUMMARY:-content verify ok}"
else
  write_state false "content verify exit $RC: ${SUMMARY:-see kopia-content-verify syslog}"
  exit 1
fi
