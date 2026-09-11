#!/usr/bin/env bash
# etcd-drill-guest-run.sh — the half of the etcd restore drill that runs INSIDE the throwaway
# guest. Copied in by etcd-restore-drill-vm (roles/setup/hypervisor) together with the k3s
# binary, the cluster token and the R2 env file, at the paths scripts/backup/etcd_restore_drill.sh
# already reads — so that script runs here unmodified, as /usr/local/bin/etcd-restore-drill.
#
# WHY THE SNAPSHOT IS FETCHED WITH CURL AND DRILLED AS A LOCAL FILE. The drill's own S3 mode
# hands `--etcd-s3` to `k3s server --cluster-reset`, and k3s then downloads the snapshot into
# <data-dir>/server/db/snapshots and joins that absolute path onto the same directory a second
# time (measured 2026-08-22, the drill script's header item 4). The script's `--local-snapshot`
# is the documented way around it: fetch once, drill the file. The listing leg still runs first
# through k3s itself (`--list-only`), so the credentials, bucket and folder are exercised by the
# same binary that will restore; the download is a plain SigV4 GET of the object the listing
# named. The off-box leg is therefore proven twice over, by two independent clients.
#
# Exit code is the drill's own: 0 only when the snapshot restored and served its objects.
set -uo pipefail

S3_ENV=/etc/rancher/k3s/etcd-s3.env
DRILL=/usr/local/bin/etcd-restore-drill
DOWNLOAD_DIR=/var/tmp/etcd-drill-download

die() { echo "etcd-drill-guest-run: $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "must run as root"
[[ -x "$DRILL" ]] || die "$DRILL is missing — the orchestrator did not stage it"
[[ -r "$S3_ENV" ]] || die "$S3_ENV is missing — the orchestrator did not stage it"

echo "== k3s binary: $(k3s --version 2>/dev/null | head -1)"

echo "== listing off-box snapshots through k3s"
listing="$("$DRILL" --list-only 2>&1)" || { printf '%s\n' "$listing"; die "the listing leg failed"; }
printf '%s\n' "$listing"
snapshot="$(printf '%s\n' "$listing" | sed -n 's/.*drilling snapshot: //p' | head -1)"
[[ -n "$snapshot" ]] || die "could not read the snapshot name out of the listing"

# shellcheck source=/dev/null
. "$S3_ENV"
: "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}" "${ETCD_S3_BUCKET:?}" "${ETCD_S3_ENDPOINT:?}"

echo "== downloading $snapshot"
install -d -m 700 "$DOWNLOAD_DIR"
# Credentials go in through a config file on stdin, never argv — the same shape kuma-push-lib.sh
# uses for its push token. Region `auto` is what R2 signs against.
if ! printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" |
     curl -fsS --max-time 300 -K - \
       --aws-sigv4 "aws:amz:auto:s3" \
       -o "$DOWNLOAD_DIR/$snapshot" \
       "https://${ETCD_S3_ENDPOINT}/${ETCD_S3_BUCKET}/etcd-snapshots/${snapshot}"; then
  die "download of $snapshot failed"
fi
echo "== downloaded $(stat -c %s "$DOWNLOAD_DIR/$snapshot") bytes"

echo "== full drill against the downloaded file"
# --keep so the scratch dir's restore.log and server.log are still there for the orchestrator
# to pull out as evidence. The whole guest is destroyed afterwards, so nothing is retained here.
"$DRILL" --local-snapshot "$DOWNLOAD_DIR/$snapshot" --keep
