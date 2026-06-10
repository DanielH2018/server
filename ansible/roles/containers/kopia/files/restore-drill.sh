#!/usr/bin/env bash
# Monthly backup RESTORE drill — managed by Ansible (kopia role); edits overwritten.
#
# "A backup you've never restored isn't a backup": the weekly `kopia snapshot verify`
# proves stored blobs are readable; this proves a restore actually reproduces a
# service's files. Each month it restores ONE service dir (rotating list below) from
# the latest snapshot into a scratch dir inside the kopia container and asserts the
# result is sane (restore exit 0, docker-compose.yml sentinel, file-count floor).
#
# Reporting: writes {"ts", "ok", "msg"} to STATE; monitor-bridge's restore_drill
# check reads it (read-only bind mount) and pushes the "Backup Restore Drill" Kuma
# monitor every cycle — failure, staleness, or a missing state file all alert.
set -uo pipefail

# Stateful services worth proving restorable; month number picks one, so the set
# cycles roughly twice a year. All have docker-compose.yml at their dir root.
SVCS=(authelia traefik n8n karakeep freshrss grafana pihole)
SVC="${SVCS[$(( $(date +%-m) % ${#SVCS[@]} ))]}"
DEST=/tmp/restore-drill
STATE=/var/lib/kopia-restore-drill/state.json

write_state() { # ok msg
  printf '{"ts": %s, "ok": %s, "msg": "%s"}\n' "$(date +%s)" "$1" "$2" > "$STATE"
  logger -t kopia-restore-drill "$1: $2"
}
fail() {
  docker exec kopia sh -c "rm -rf $DEST" 2>/dev/null
  write_state false "$1"
  exit 1
}

ROOT=$(docker exec kopia kopia snapshot list --json 2>/dev/null | jq -r '.[-1].rootEntry.obj')
[ -n "$ROOT" ] && [ "$ROOT" != "null" ] || fail "could not resolve latest snapshot root"

docker exec kopia sh -c "rm -rf $DEST" 2>/dev/null
docker exec kopia kopia restore "$ROOT/$SVC" "$DEST" >/dev/null 2>&1 \
  || fail "kopia restore of $SVC from $ROOT failed"

docker exec kopia sh -c "test -f $DEST/docker-compose.yml" \
  || fail "$SVC restore missing docker-compose.yml sentinel"
FILES=$(docker exec kopia sh -c "find $DEST -type f | wc -l" | tr -d '[:space:]')
[ "${FILES:-0}" -ge 3 ] || fail "$SVC restore implausibly small ($FILES files)"
BYTES=$(docker exec kopia sh -c "du -sk $DEST | cut -f1" | tr -d '[:space:]')

docker exec kopia sh -c "rm -rf $DEST"
write_state true "restored $SVC from latest snapshot: $FILES files, ${BYTES}K"
