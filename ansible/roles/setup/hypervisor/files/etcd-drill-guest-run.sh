#!/usr/bin/env bash
# etcd-drill-guest-run.sh — the half of the etcd restore drill that runs INSIDE the throwaway
# guest. Copied in by etcd-restore-drill-vm (roles/setup/hypervisor) together with the k3s
# binary, the cluster token and the R2 env file, at the paths scripts/backup/etcd_restore_drill.sh
# already reads — so that script runs here unmodified, as /usr/local/bin/etcd-restore-drill.
#
# WHY THE GUEST LISTS AND FETCHES WITH CURL, AND DRILLS A LOCAL FILE. Two k3s behaviours rule
# out the drill script's own S3 mode here:
#   - `k3s etcd-snapshot list` does not talk to S3 itself. Since k3s 1.29 the etcd-snapshot
#     subcommands authenticate to a RUNNING k3s server's supervisor API and list through it.
#     A server-less host fails at `open .../server/token`, and this guest — token staged, no
#     server — fails at the connection (measured 2026-09-11, the first run in the guest).
#   - `--etcd-s3` handed to `k3s server --cluster-reset` downloads the snapshot into
#     <data-dir>/server/db/snapshots and joins that absolute path onto the same directory a
#     second time (measured 2026-08-22, the drill script's header item 4).
# So the guest lists the bucket with a SigV4 ListObjectsV2, downloads the newest offbox-*
# object with a SigV4 GET, and hands the file to the drill's `--local-snapshot`, which is the
# script's documented way around the second point. The credentials, bucket and folder are
# still exercised before anything is restored; the restore itself is the k3s leg.
#
# Exit code is the drill's own: 0 only when the snapshot restored and served its objects.
set -uo pipefail

S3_ENV=/etc/rancher/k3s/etcd-s3.env
DRILL=/usr/local/bin/etcd-restore-drill
DOWNLOAD_DIR=/var/tmp/etcd-drill-download
# Where a --detached run leaves its output and exit code for the orchestrator to poll.
OUT=/var/tmp/etcd-drill.out
RC=/var/tmp/etcd-drill.rc

die() { echo "etcd-drill-guest-run: $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "must run as root"

# --detached: start the real run in the background, owning none of the caller's descriptors, and
# return at once. The orchestrator polls $RC over fresh ssh sessions instead of holding one open
# for the whole run — a held session hung for 20 minutes after the drill had died (run
# 20260911T135526Z), and a hung session is exactly what the orchestrator cannot diagnose.
if [[ "${1:-}" == "--detached" ]]; then
  rm -f "$RC" "$OUT"
  nohup sh -c "$0 >$OUT 2>&1; echo \$? >$RC" >/dev/null 2>&1 </dev/null &
  echo "started (pid $!); output in $OUT, exit code in $RC when done"
  exit 0
fi
[[ -x "$DRILL" ]] || die "$DRILL is missing — the orchestrator did not stage it"
[[ -r "$S3_ENV" ]] || die "$S3_ENV is missing — the orchestrator did not stage it"

echo "== k3s binary: $(k3s --version 2>/dev/null | head -1)"

# shellcheck source=/dev/null
. "$S3_ENV"
: "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}" "${ETCD_S3_BUCKET:?}" "${ETCD_S3_ENDPOINT:?}"

# Credentials go in through a config file on stdin, never argv — the same shape kuma-push-lib.sh
# uses for its push token. Region `auto` is what R2 signs against.
r2_get() {
  printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" |
    curl -fsS --max-time 300 -K - --aws-sigv4 "aws:amz:auto:s3" "$@"
}

echo "== listing off-box snapshots in s3://${ETCD_S3_BUCKET}/etcd-snapshots"
listing="$(r2_get "https://${ETCD_S3_ENDPOINT}/${ETCD_S3_BUCKET}/?list-type=2&prefix=etcd-snapshots/offbox-")" \
  || die "the listing leg failed (ListObjectsV2 against ${ETCD_S3_ENDPOINT})"
snapshot="$(printf '%s\n' "$listing" | grep -o '<Key>etcd-snapshots/offbox-[^<]*</Key>' \
            | sed 's|<Key>etcd-snapshots/||; s|</Key>||' | sort | tail -1)"
[[ -n "$snapshot" ]] || die "no offbox-* object under etcd-snapshots/ (listing: ${listing:0:300})"
echo "== newest off-box snapshot: $snapshot"

echo "== downloading $snapshot"
install -d -m 700 "$DOWNLOAD_DIR"
r2_get -o "$DOWNLOAD_DIR/$snapshot" "https://${ETCD_S3_ENDPOINT}/${ETCD_S3_BUCKET}/etcd-snapshots/${snapshot}" \
  || die "download of $snapshot failed"
echo "== downloaded $(stat -c %s "$DOWNLOAD_DIR/$snapshot") bytes"

echo "== full drill against the downloaded file"
# --keep so the scratch dir's restore.log and server.log are still there for the orchestrator
# to pull out as evidence. The whole guest is destroyed afterwards, so nothing is retained here.
"$DRILL" --local-snapshot "$DOWNLOAD_DIR/$snapshot" --keep
