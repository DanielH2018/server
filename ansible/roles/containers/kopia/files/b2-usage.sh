#!/usr/bin/env bash
# Daily B2 billable-usage probe — managed by Ansible (kopia role); edits overwritten.
#
# The repo lives in a Backblaze B2 free-tier bucket (10 GB cap), so running out of
# space silently kills backups. Billable bytes include HIDDEN file versions (the repo
# speaks B2's S3 endpoint, where deletes only hide; lifecycle purges after 7 days —
# see the role CLAUDE.md), so `kopia blob stats` UNDERCOUNTS what B2 charges for.
# `rclone size --b2-versions` counts every stored version, matching the B2 dashboard.
# rclone ships in the kopia image and creds come from the repo's own connection
# config at runtime — no credentials are stored anywhere new.
#
# Reporting: writes {"ts","ok","bytes","msg"} to STATE; monitor-bridge's b2_usage
# check reads it (read-only bind mount) and pushes the "B2 Storage Usage" Kuma
# monitor every cycle — over-threshold, staleness, or a missing state file all alert.
set -uo pipefail

STATE=/var/lib/kopia-b2-usage/state.json
CFG=/app/config/repository.config

write_state() { # ok bytes msg
  printf '{"ts": %s, "ok": %s, "bytes": %s, "msg": "%s"}\n' "$(date +%s)" "$1" "$2" "$3" > "$STATE"
  logger -t kopia-b2-usage "$1: $3"
}
fail() { write_state false 0 "$1"; exit 1; }

CONF=$(docker exec kopia cat "$CFG" 2>/dev/null) \
  || fail "cannot read repository.config from the kopia container"
ACCOUNT=$(jq -r '.storage.config.accessKeyID // empty' <<<"$CONF")
KEY=$(jq -r '.storage.config.secretAccessKey // empty' <<<"$CONF")
BUCKET=$(jq -r '.storage.config.bucket // empty' <<<"$CONF")
[ -n "$ACCOUNT" ] && [ -n "$KEY" ] && [ -n "$BUCKET" ] \
  || fail "repository.config missing s3 credentials/bucket"

# B2 S3-key pairs are the same credentials as native-API application keys, so the
# rclone b2 backend (which understands --b2-versions) accepts them directly.
BYTES=$(docker exec \
  -e RCLONE_CONFIG_B2_TYPE=b2 \
  -e RCLONE_CONFIG_B2_ACCOUNT="$ACCOUNT" \
  -e RCLONE_CONFIG_B2_KEY="$KEY" \
  kopia rclone size "b2:$BUCKET" --b2-versions --json 2>/dev/null | jq -r '.bytes // empty')
[ -n "$BYTES" ] || fail "rclone size query against b2:$BUCKET failed"

# Also expose the billable bytes as a Prometheus gauge via the node-exporter textfile collector,
# so Grafana can graph the B2 usage TREND (the Kuma monitor is a binary 85% alert with no runway
# curve). Written atomically (temp + mv in the same dir) so a scrape can't read a half-written
# file; the temp suffix isn't `.prom` so the collector ignores it mid-write. The dir is created +
# bind-mounted ro into node-exporter by the prometheus role — guard on its existence so a host
# without it (or before prometheus is deployed) just skips silently.
TEXTFILE_DIR=/var/lib/node-exporter-textfile
if [ -d "$TEXTFILE_DIR" ]; then
  TMP=$(mktemp "$TEXTFILE_DIR/kopia_b2.prom.XXXXXX") && {
    printf '# HELP kopia_b2_billable_bytes Billable bytes in the Kopia B2 bucket (incl. hidden versions).\n'
    printf '# TYPE kopia_b2_billable_bytes gauge\n'
    printf 'kopia_b2_billable_bytes %s\n' "$BYTES"
  } > "$TMP" && chmod 0644 "$TMP" && mv "$TMP" "$TEXTFILE_DIR/kopia_b2.prom"
  # 0644: node-exporter runs as `nobody`, and mktemp creates 0600 — without this the collector
  # gets "permission denied" (node_textfile_scrape_error 1) and the gauge never appears.
fi

write_state true "$BYTES" \
  "$(awk -v b="$BYTES" 'BEGIN{printf "%.2fGB billable in B2 (incl. hidden versions)", b/1e9}')"
