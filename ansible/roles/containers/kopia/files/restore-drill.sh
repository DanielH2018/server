#!/usr/bin/env bash
# Monthly backup RESTORE drill — managed by Ansible (kopia role); edits overwritten.
#
# "A backup you've never restored isn't a backup": the weekly `kopia snapshot verify`
# proves stored blobs are readable; this proves a restore actually reproduces a service's
# files. Each run restores ONE rotating service dir from a snapshot into a scratch dir
# inside the kopia container and asserts the result is sane.
#
# What it asserts (beyond "restore exit 0"):
#   - a SERVICE-SPECIFIC state file reappears (not just any docker-compose.yml — that would
#     clear the floor on a wrong/partial restore; a real state file proves the right tree
#     with real data came back),
#   - a file-count floor.
# Plus two backup-integrity guards:
#   - the LATEST snapshot is fresh (scheduler not stalled), regardless of which snapshot we
#     restore — otherwise the drill could keep passing forever on an aging backup,
#   - QUARTERLY it restores the OLDEST retained snapshot instead of the newest, to exercise
#     the retention tail (the actual disaster-recovery case), not just yesterday's data.
#
# Reporting: writes {"ts","ok","msg"} to STATE; monitor-bridge's restore_drill check reads
# it (RO bind mount) and pushes the "Backup Restore Drill" Kuma monitor every cycle —
# failure, staleness, or a missing state file all alert.
set -uo pipefail

# Stateful services worth proving restorable, each paired with a service-SPECIFIC state
# file that must reappear after restore. All verified present in the snapshot.
SVCS=(authelia traefik n8n karakeep freshrss grafana pihole home-assistant zigbee2mqtt)
declare -A SENTINEL=(
  [authelia]=config/configuration.yml
  [traefik]=data/acme.json
  [n8n]=data/config
  [karakeep]=data/db.db
  [freshrss]=config/www/freshrss/data/config.php
  [grafana]=data/grafana.db
  [pihole]=data/etc-pihole/pihole.toml
  # HA's .storage registry (device/entity/Z2M pairings) is the highest-value, hardest-to-
  # rebuild tree in the homelab — prove it restores, not just that it's backed up. The
  # registry is a JSON file (not *.db), so it skips the SQLite-magic branch below.
  [home-assistant]=config/.storage/core.device_registry
  # Z2M's OWN database.db is NDJSON (newline-delimited JSON), not SQLite — the *.db
  # SQLite-magic branch below would false-fail against its header. coordinator_backup.json
  # (the Zigbee network's radio-side backup, re-pair-critical) is a real JSON file and
  # skips that branch the same way home-assistant's registry does.
  [zigbee2mqtt]=data/coordinator_backup.json
)
# Rotation: + year term so the singly-covered slot moves year over year. With a bare
# month % len, the services in the wrap-around (months > len) are drilled only once a year
# while the rest get twice — authelia (the SSO root of trust) shouldn't be the perennial
# odd one out, so fold the year in to rotate which service draws the short straw.
M=$(date +%-m); Y=$(date +%Y)
SVC="${SVCS[$(( (M + Y) % ${#SVCS[@]} ))]}"
SENT="${SENTINEL[$SVC]}"
DEST=/tmp/restore-drill
STATE=/var/lib/kopia-restore-drill/state.json

write_state() { # ok msg
  # jq, not printf: a stray backslash/control char in the msg would make a hand-built
  # string invalid JSON -> monitor-bridge reads "state unparseable" (false DOWN).
  jq -nc --argjson ts "$(date +%s)" --argjson ok "$1" --arg msg "$2" \
    '{ts: $ts, ok: $ok, msg: $msg}' > "$STATE"
  logger -t kopia-restore-drill "$1: $2"
}
fail() {
  docker exec kopia sh -c "rm -rf $DEST" 2>/dev/null
  write_state false "$1"
  exit 1
}

SNAPS=$(docker exec kopia kopia snapshot list --json 2>/dev/null)
[ -n "$SNAPS" ] || fail "could not list snapshots"

# Scheduler-health guard: the LATEST snapshot must be recent (daily cadence; 48h tolerates
# one missed run), regardless of which snapshot we restore below.
LATEST_EPOCH=$(date -d "$(printf '%s' "$SNAPS" | jq -r '.[-1].startTime')" +%s 2>/dev/null || echo 0)
AGE_H=$(( ( $(date +%s) - LATEST_EPOCH ) / 3600 ))
{ [ "$LATEST_EPOCH" -gt 0 ] && [ "$AGE_H" -lt 48 ]; } \
  || fail "latest snapshot is stale (${AGE_H}h old) — snapshot scheduler may have stalled"

# Quarterly (months divisible by 3) restore the OLDEST retained snapshot to exercise the
# retention tail; otherwise the newest. jq supports the negative index for "latest".
if [ $(( M % 3 )) -eq 0 ]; then IDX=0; WHICH=oldest; else IDX=-1; WHICH=latest; fi
ROOT=$(printf '%s' "$SNAPS" | jq -r ".[$IDX].rootEntry.obj")
SNAP_TS=$(printf '%s' "$SNAPS" | jq -r ".[$IDX].startTime")
{ [ -n "$ROOT" ] && [ "$ROOT" != "null" ]; } || fail "could not resolve $WHICH snapshot root"

docker exec kopia sh -c "rm -rf $DEST" 2>/dev/null
docker exec kopia kopia restore "$ROOT/$SVC" "$DEST" >/dev/null 2>&1 \
  || fail "kopia restore of $SVC from $WHICH snapshot ($ROOT) failed"

docker exec kopia sh -c "test -f '$DEST/$SENT'" \
  || fail "$SVC restore missing service-specific sentinel '$SENT' (from $WHICH snapshot)"

# For SQLite sentinels (karakeep db.db, grafana grafana.db) confirm the restored file is a
# structurally valid database, not just present — guards against a wrong/empty/truncated file
# landing at the sentinel path. The image has no sqlite3 for a PRAGMA integrity_check, so
# check the 16-byte header magic ("SQLite format 3\0"); the restore already re-decrypts every
# blob, so byte-level corruption would have failed the restore above.
case "$SENT" in
  *.db)
    MAGIC=$(docker exec kopia sh -c "head -c 15 '$DEST/$SENT'" 2>/dev/null)
    [ "$MAGIC" = "SQLite format 3" ] \
      || fail "$SVC sentinel '$SENT' restored but is not a valid SQLite database (header: '$MAGIC')"
    ;;
esac

FILES=$(docker exec kopia sh -c "find $DEST -type f | wc -l" | tr -d '[:space:]')
[ "${FILES:-0}" -ge 3 ] || fail "$SVC restore implausibly small ($FILES files)"
BYTES=$(docker exec kopia sh -c "du -sk $DEST | cut -f1" | tr -d '[:space:]')

docker exec kopia sh -c "rm -rf $DEST"
write_state true "restored $SVC from $WHICH snapshot ($SNAP_TS): sentinel $SENT ok, $FILES files, ${BYTES}K"
