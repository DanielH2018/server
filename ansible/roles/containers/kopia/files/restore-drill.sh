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
# shellcheck source=/dev/null
source /usr/local/lib/kopia-lib.sh

# Stateful services worth proving restorable, each paired with a service-SPECIFIC state
# file that must reappear after restore. All verified present in the snapshot.
# freshrss dropped 2026-08-06 — it cut over to k3s on 08-05, so this drill would have restored a
# frozen directory and passed, proving nothing about the live data (now a Longhorn PVC, covered by
# longhorn-backup-health.sh's per-volume check). karakeep and n8n dropped the same day, on their own
# cutovers, for the same reason. Drop a service here when it migrates.
# zigbee2mqtt dropped 2026-08-09 — slice-5 B2 moved it to k3s; its data dir on this host is
# the frozen rollback copy, so drilling it would prove nothing about the live pairings (now a
# Longhorn PVC on the backed-up class, asserted by longhorn-backup-health.sh).
SVCS=(authelia traefik grafana pihole home-assistant wg-easy)
declare -A SENTINEL=(
  [authelia]=config/configuration.yml
  [traefik]=data/acme.json
  [grafana]=data/grafana.db
  [pihole]=data/etc-pihole/pihole.toml
  # sonarr was the *arr sentinel here until slice 4's B4c cutover (2026-08-07) moved it to
  # k3s. jellyfin followed on 2026-08-08 (B5) — its sentinel was config/data/data/jellyfin.db,
  # the users / watch history / API keys / library definitions. Both dropped rather than
  # repointed, for the same reason: this drill restores from the daniel-server snapshot, where
  # those databases no longer change, so it would have gone on passing against a frozen file
  # forever — a green check asserting nothing. The cluster's config PVCs are Longhorn-backed and
  # longhorn-backup-health.sh asserts their freshness cluster-side instead.
  #
  # jellyfin used to be what exercised the SQLite-magic branch below. grafana's data/grafana.db
  # still does, so that coverage survives its departure — but it is now the ONLY sentinel that
  # does, so don't drop it without providing another.
  # wg-easy's sentinel is the PULLED Pi peer config (pi-peers/, filled by the daniel-server
  # wg-easy-pull-pi-peers cron) — the one un-rebuildable secret backup, so prove IT restores,
  # not just the server's own re-templatable config. wg0.json is a real JSON file, so it skips
  # the SQLite-magic branch below like home-assistant's/zigbee2mqtt's JSON sentinels.
  [wg-easy]=pi-peers/wg0.json
  # HA's .storage registry (device/entity/Z2M pairings) is the highest-value, hardest-to-
  # rebuild tree in the homelab — prove it restores, not just that it's backed up. The
  # registry is a JSON file (not *.db), so it skips the SQLite-magic branch below.
  [home-assistant]=config/.storage/core.device_registry
)
# Rotation: (month + year) % len picks one service per monthly run. With 12 services and 12
# months each is drilled once a year; the + year term shifts which calendar month a given
# service lands on year over year, so none is perennially stuck in the same (e.g. reboot-heavy)
# month — and the state file is rewritten every month regardless, so the freshness monitor
# stays green between a service's yearly turns.
M=$(date +%-m); Y=$(date +%Y)
SVC="${SVCS[$(( (M + Y) % ${#SVCS[@]} ))]}"
SENT="${SENTINEL[$SVC]}"
DEST=/tmp/restore-drill
STATE=/var/lib/kopia-restore-drill/state.json

write_state() { kopia_write_state "$STATE" kopia-restore-drill "$@"; } # ok msg
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

# For SQLite sentinels (grafana grafana.db, jellyfin jellyfin.db, …) confirm the restored file is a
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
