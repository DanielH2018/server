#!/usr/bin/env bash
#
# etcd_restore_drill.sh — prove an off-box etcd snapshot actually restores, without an outage.
#
# docs/k3s-etcd-restore.md documents a `--cluster-reset` on daniel-box. That is a real restore:
# it stops k3s, rolls the whole cluster back to the snapshot's moment, loses everything created
# since, and needs daniel-server's agent certs deleted so it can rejoin. Nobody was ever going
# to run that for practice, which is why the doc carried "procedure documented, NOT drilled"
# from the day it was written (2026-08-16). An undrilled restore path is a restore path you
# find out about during the incident.
#
# This drills the same snapshot with none of that. It restores into a THROWAWAY data-dir and
# brings up an API server that serves it, then reads the objects back and tears the whole thing
# down. The live cluster is never stopped, never reset, and never contacted.
#
# WHY IT USES k3s RATHER THAN etcdutl. The obvious shape is `etcdutl snapshot status` plus a
# standalone etcd. Neither etcdutl nor etcdctl is installed here, and fetching one introduces a
# version-skew question against whatever etcd k3s embeds — a drill whose own tooling might be
# the thing that fails is not evidence. `k3s server --cluster-reset` is the same binary that
# WROTE the snapshot, so the format question cannot arise.
#
# WHY IT CANNOT COLLIDE WITH PRODUCTION:
#   --data-dir      a fresh directory under /var/tmp; the real one is never opened
#   --disable-agent no kubelet, no CNI, no iptables rules, no pods scheduled
#   --https-listen-port / --supervisor-port  alternate ports, bound to 127.0.0.1
#   --disable       every packaged component off; this serves objects, it does not run them
# The script refuses outright if the resolved data-dir is the live one.
#
# WHAT IT PROVES, AND WHAT IT CANNOT. It proves the snapshot is complete, readable by this k3s
# version, and that the Kubernetes objects come back — namespaces, workloads, PVCs, the object
# graph a rebuild depends on. It does NOT prove Secrets are usable: since 2026-08-20 those are
# encrypted at rest, and this drill runs against a host whose own copy of the key is already in
# place, so it never exercises the path a rebuilt host takes. The drill reports Secret COUNT
# (presence) and deliberately never decodes one.
#
# Corrected 2026-08-23: this comment used to say the key "is not in the snapshot". It is — the
# contents of encryption-config.json ride inside the snapshot's /bootstrap blob (verified against
# k3s v1.36.3+k3s1), encrypted with the cluster token. The artifact that has to survive daniel-box
# is therefore /var/lib/rancher/k3s/server/token, which this script already depends on for exactly
# that reason (LIVE_TOKEN below, and the --cluster-reset note further down). An out-of-band copy
# was taken 2026-08-23; docs/k3s-etcd-restore.md carries the evidence and the re-verify command.
#
# STATUS, 2026-08-22. `--list-only` WORKS and is the useful part today: it proved the off-box
# leg end to end for the first time — credentials, bucket, folder, download and decompression
# of offbox-daniel-box-1787366702.zip, the 02:45 snapshot. The FULL drill does NOT yet pass on
# daniel-box, and the reason is structural rather than a bug in this script: `k3s server
# --cluster-reset` assumes it is the only k3s on the host, and every workaround here found the
# next thing it assumes. In order:
#
#   1. it needs <data-dir>/server/token to EXIST; --token-file does not satisfy it
#   2. the reset stage starts its own listeners, so isolation flags belong on BOTH invocations
#   3. --disable-agent does not stop the supervisor client load-balancer on 127.0.0.1:6444;
#      --lb-server-port is the flag
#   4. --cluster-reset-restore-path is a NAME relative to <data-dir>/server/db/snapshots, always
#      joined onto it — so an absolute path doubles, and --etcd-s3 doubles it for you by feeding
#      its own download path back through the join
#   5. after all four, the run wedges in "Waiting to retrieve agent configuration; server is not
#      ready" — 17 minutes on 6 seconds of CPU, against ~60s when it resolves
#
# Nothing in that list is fixable from out here, so DON'T keep patching this script against a
# live k3s. Two paths actually finish the job, and both are in docs/k3s-etcd-restore.md: run
# this on a host with no k3s of its own (a throwaway VM, where every item above evaporates),
# or take the scheduled outage and do the real documented restore. Throughout all five
# failures the live cluster stayed Ready and every write landed in /var/tmp — the isolation
# held, which is the one thing worth trusting from this exercise.
#
# Usage:
#   sudo ./scripts/etcd_restore_drill.sh --list-only      # the part that works: prove the R2 leg
#   sudo ./scripts/etcd_restore_drill.sh                 # newest off-box snapshot in R2
#   sudo ./scripts/etcd_restore_drill.sh --snapshot NAME # a specific one from the S3 listing
#   sudo ./scripts/etcd_restore_drill.sh --keep          # leave the scratch dir for poking at
#
# Exit 0 = the snapshot restored and served objects. Non-zero = it did not, and the message
# says which stage. Run it after any change to the snapshot cron, and periodically regardless:
# the value is in the last time it passed, not in it existing.

set -euo pipefail

LIVE_DATA_DIR=/var/lib/rancher/k3s
# Read-only, and the only thing this drill takes from the live installation. `--cluster-reset`
# derives the restore's encryption material from the cluster token, and a scratch data-dir has
# no `server/token` of its own — the first run failed with exactly that. The token is passed to
# k3s as an argument-free file reference so it never lands in argv or in this script's output.
LIVE_TOKEN=/var/lib/rancher/k3s/server/token
S3_ENV=/etc/rancher/k3s/etcd-s3.env
SCRATCH="/var/tmp/etcd-restore-drill.$$"
PORT=7443
SUPERVISOR_PORT=7444
LB_PORT=7445
KEEP=0
LIST_ONLY=0
SNAPSHOT=""
LOCAL_SNAPSHOT=""
# Generous: a cold API server on a busy box takes longer than a warm one, and a false
# "did not come up" is the failure mode that would get this drill ignored.
READY_TIMEOUT=180
# Where a passing run records itself. Until 2026-08-23 a pass was recorded only by the operator
# editing docs/k3s-etcd-restore.md by hand, which means "when did this last pass" had no
# machine-readable answer and nothing could detect the drill silently stopping. The Longhorn
# restore drill beside it already learned this (its stamp dir is read by checks 7 and 8 of
# longhorn-backup-health.sh, added after that drill spent months as an unscheduled one-off in a
# home directory). Two modes stamp separately and MUST NOT be conflated: `list-only` proves the
# off-box leg — credentials, bucket, folder, download, decompression — while `full` additionally
# proves the object graph comes back. A watchdog that accepted a list-only stamp as drill
# coverage would be the "one tier hiding behind another tier's evidence" shape this estate has
# already been bitten by, so the mode is written into the stamp, not just the timestamp.
STAMP_DIR="${ETCD_DRILL_STAMP_DIR:-/var/lib/etcd-restore-drill}"
# The restore stage needs its own bound. Measured 2026-08-22: a run wedged in k3s's
# "Waiting to retrieve agent configuration; server is not ready" retry loop sat for 17
# minutes on 6 seconds of CPU and would have sat there indefinitely — a drill with no
# ceiling is a drill that hangs a terminal rather than reporting a result. 600s is well past
# the ~60s the loop takes when it does resolve.
RESTORE_TIMEOUT=600

die() { echo "etcd-restore-drill: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Record a pass. $1 is the mode — `list-only` or `full` — and it is written into the file so a
# reader cannot mistake the cheaper proof for the stronger one. Best-effort: a drill that passed
# must not be reported as failed because its stamp could not be written, so failures here warn.
stamp_success() {
  local mode="$1"
  mkdir -p "$STAMP_DIR" 2>/dev/null || { echo "warning: cannot create $STAMP_DIR" >&2; return 0; }
  chmod 0755 "$STAMP_DIR" 2>/dev/null || true
  printf 'mode=%s\nsnapshot=%s\nutc=%s\nepoch=%s\n' \
    "$mode" "${SNAPSHOT:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(date -u +%s)" \
    > "${STAMP_DIR}/last-success-${mode}" \
    || echo "warning: could not write the ${mode} stamp" >&2
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT="${2:?--snapshot needs a name}"; shift 2 ;;
    # Restore from a snapshot file already on disk, skipping S3 entirely. This is not a
    # convenience: with --etcd-s3, k3s downloads to <data-dir>/server/db/snapshots/<name>,
    # rewrites the restore path to that ABSOLUTE path, and then joins it with the snapshots
    # directory a second time — measured 2026-08-22:
    #   open /var/tmp/<dir>/server/db/snapshots/var/tmp/<dir>/server/db/snapshots/<name>.zip
    # The download and decompression both succeed first, so the off-box leg is proven either
    # way; it is only the restore that cannot be driven straight from S3 into a non-default
    # data-dir. Fetch once, then drill the file.
    --local-snapshot) LOCAL_SNAPSHOT="${2:?--local-snapshot needs a path}"; shift 2 ;;
    --keep)     KEEP=1; shift ;;
    # Stops after the listing, which exercises the credentials, bucket and folder and touches
    # nothing. Run this form first on any host where the drill has not run before: it proves the
    # off-box leg without ever reaching `k3s server --cluster-reset`.
    --list-only) LIST_ONLY=1; shift ;;
    -h|--help)  sed -n '2,50p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "must run as root (k3s server needs it); use sudo"
[[ -r "$S3_ENV" ]] || die "cannot read $S3_ENV — this host does not hold the R2 credentials"
[[ -r "$LIVE_TOKEN" ]] || die "cannot read $LIVE_TOKEN — run this on the k3s server node"
command -v k3s >/dev/null || die "k3s not on PATH"

# The one guard that matters. Everything else in this script is reversible; opening the live
# data-dir with --cluster-reset is not.
case "$SCRATCH" in
  "$LIVE_DATA_DIR"*) die "refusing: scratch dir resolves inside the live data-dir" ;;
esac

# A root-only env file rendered by the k3s role, so it is not in this repo for shellcheck to
# follow. Sourced with `set -a` so AWS_* reach k3s as environment rather than as argv.
set -a
# shellcheck source=/dev/null
. "$S3_ENV"
set +a
: "${ETCD_S3_BUCKET:?not set in $S3_ENV}"
: "${ETCD_S3_ENDPOINT:?not set in $S3_ENV}"

S3_ARGS=(--etcd-s3
         --etcd-s3-bucket "$ETCD_S3_BUCKET"
         --etcd-s3-endpoint "$ETCD_S3_ENDPOINT"
         --etcd-s3-folder etcd-snapshots
         --etcd-s3-region auto)

# Every port and path that could touch the live installation, in ONE array applied to BOTH
# k3s invocations. Passing them only to the server start is not enough and fails loudly:
# `--cluster-reset` brings up the internal apiserver load balancer too, and on the default
# ports it hits `listen tcp 127.0.0.1:6444: bind: address already in use` against the running
# server (measured 2026-08-22). A restore stage that binds the live supervisor port is the
# trap this drill exists to stay clear of, so the isolation cannot live on one call site.
ISOLATION_ARGS=(--data-dir "$SCRATCH"
                --disable-agent
                --https-listen-port "$PORT"
                --supervisor-port "$SUPERVISOR_PORT"
                # A THIRD listener, separate from the API server and the supervisor, and the
                # one that actually collided: the supervisor client load-balancer binds
                # 127.0.0.1:6444 and `--disable-agent` does not stop it. Moving the supervisor
                # port alone is not enough — measured 2026-08-22, where the reset reached
                # `dynamiclistener 127.0.0.1:7444` and still died on 6444.
                --lb-server-port "$LB_PORT"
                --bind-address 127.0.0.1
                --advertise-address 127.0.0.1)

cleanup() {
  local rc=$?
  # A failed drill keeps its scratch dir. The first run of this script died pointing at
  # restore.log and then deleted it on the way out, which is the same shape as every other
  # unreadable-evidence trap in this repo: the diagnosis and the thing that explains it must
  # not be removed by the same exit path.
  local why="--keep"
  if [[ $rc -ne 0 ]]; then KEEP=1; why="the drill failed; its logs are the evidence"; fi
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    log "stopping the scratch API server (pid $SERVER_PID)"
    kill "$SERVER_PID" 2>/dev/null || true
    # Wait on the PID rather than polling by name: a pgrep loop matches itself and never exits.
    tail --pid="$SERVER_PID" -f /dev/null 2>/dev/null || true
  fi
  if [[ "$KEEP" == "1" ]]; then
    log "leaving scratch dir at $SCRATCH ($why)"
  else
    rm -rf "$SCRATCH"
  fi
  exit $rc
}
trap cleanup EXIT INT TERM

RESTORE_S3_ARGS=("${S3_ARGS[@]}")
if [[ -n "$LOCAL_SNAPSHOT" ]]; then
  [[ -r "$LOCAL_SNAPSHOT" ]] || die "cannot read $LOCAL_SNAPSHOT"
  # `--cluster-reset-restore-path` is a NAME relative to <data-dir>/server/db/snapshots, not a
  # path — k3s joins it onto that directory unconditionally. An absolute path therefore always
  # produces the doubled form, measured twice on 2026-08-22:
  #   open <scratch-A>/server/db/snapshots/<scratch-B>/server/db/snapshots/<name>.zip
  # So stage the file into this run's own snapshots dir and pass the bare filename. The same
  # join is why --etcd-s3 fails here: k3s downloads to that directory, then feeds the absolute
  # download path back through the join.
  SNAPSHOT="$(basename "$LOCAL_SNAPSHOT")"
  RESTORE_S3_ARGS=()
  log "restoring from a local file, S3 not consulted"
elif [[ -z "$SNAPSHOT" ]]; then
  log "listing off-box snapshots"
  # The listing is the drill's first assertion in its own right: it exercises the credentials,
  # the bucket and the folder before anything is downloaded.
  SNAPSHOT=$(k3s etcd-snapshot list "${S3_ARGS[@]}" 2>/dev/null \
             | awk 'NR>1 && $1 ~ /^offbox-/ {print $1}' | sort | tail -1)
  [[ -n "$SNAPSHOT" ]] || die "no offbox-* snapshot found in s3://${ETCD_S3_BUCKET}/etcd-snapshots"
fi
log "drilling snapshot: $SNAPSHOT"

if [[ "$LIST_ONLY" == "1" ]]; then
  log "--list-only: the off-box leg works (credentials, bucket, folder, a named snapshot)"
  log "nothing was restored; re-run without --list-only for the actual drill"
  stamp_success list-only
  exit 0
fi

mkdir -p "$SCRATCH/server"
# `--cluster-reset` reads the token from <data-dir>/server/token and checks for that FILE, not
# for a --token/--token-file argument: passing --token-file left the same fatal
# "server/token does not exist, please pass --token" (measured twice, 2026-08-22). Seeding the
# path is what satisfies it, and it keeps the token out of argv, where `--token <value>` would
# expose it to any `ps` on this host.
install -m 600 "$LIVE_TOKEN" "$SCRATCH/server/token"
if [[ -n "$LOCAL_SNAPSHOT" ]]; then
  install -D -m 600 "$LOCAL_SNAPSHOT" "$SCRATCH/server/db/snapshots/$SNAPSHOT"
  log "staged $SNAPSHOT into this run's snapshots dir"
fi
log "restoring into $SCRATCH (live cluster untouched)"
# --cluster-reset restores and exits; it does not stay running.
if ! timeout --signal=TERM --kill-after=30 "$RESTORE_TIMEOUT" \
     k3s server \
      --cluster-reset \
      --cluster-reset-restore-path="$SNAPSHOT" \
      "${ISOLATION_ARGS[@]}" \
      ${RESTORE_S3_ARGS[@]+"${RESTORE_S3_ARGS[@]}"} >"$SCRATCH/restore.log" 2>&1; then
  rc=$?
  tail -20 "$SCRATCH/restore.log" >&2
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then
    die "restore stage exceeded ${RESTORE_TIMEOUT}s and was killed. On a host already running
  k3s this usually means the wedged agent-config loop, not a bad snapshot — see the header."
  fi
  die "restore stage failed — see $SCRATCH/restore.log"
fi
log "restore stage completed"

log "starting a scratch API server on 127.0.0.1:$PORT"
k3s server \
  "${ISOLATION_ARGS[@]}" \
  --write-kubeconfig="$SCRATCH/drill.kubeconfig" \
  --write-kubeconfig-mode=600 \
  --disable=traefik,servicelb,metrics-server,local-storage \
  --disable-cloud-controller \
  --disable-network-policy \
  --flannel-backend=none >"$SCRATCH/server.log" 2>&1 &
SERVER_PID=$!

KUBECTL=(k3s kubectl --kubeconfig "$SCRATCH/drill.kubeconfig")
deadline=$((SECONDS + READY_TIMEOUT))
until "${KUBECTL[@]}" get --raw /readyz >/dev/null 2>&1; do
  kill -0 "$SERVER_PID" 2>/dev/null || { tail -20 "$SCRATCH/server.log" >&2; die "scratch server exited during startup"; }
  (( SECONDS < deadline )) || { tail -20 "$SCRATCH/server.log" >&2; die "scratch server never became ready in ${READY_TIMEOUT}s"; }
  sleep 2
done
log "scratch API server is serving the restored objects"

# The assertions. Counts, not names: the point is that the object graph came back, and a count
# that reads zero is the failure this drill exists to catch.
ns=$("${KUBECTL[@]}" get namespaces --no-headers 2>/dev/null | wc -l)
deploys=$("${KUBECTL[@]}" get deployments -A --no-headers 2>/dev/null | wc -l)
pvcs=$("${KUBECTL[@]}" get pvc -A --no-headers 2>/dev/null | wc -l)
secrets=$("${KUBECTL[@]}" get secrets -A --no-headers 2>/dev/null | wc -l)
crds=$("${KUBECTL[@]}" get crd --no-headers 2>/dev/null | wc -l)

echo
echo "restored from : $SNAPSHOT"
echo "namespaces    : $ns"
echo "deployments   : $deploys"
echo "PVCs          : $pvcs"
echo "CRDs          : $crds"
echo "secrets       : $secrets (presence only — encrypted at rest, never decoded by this drill)"
echo

# A restore that yields an empty or near-empty object set is the silent failure worth catching:
# the stages all "succeed" and the cluster comes up carrying nothing.
[[ "$ns" -ge 3 ]]      || die "only $ns namespaces — the snapshot restored but carries no cluster"
[[ "$deploys" -ge 1 ]] || die "no deployments in the restored set"
[[ "$pvcs" -ge 1 ]]    || die "no PVCs in the restored set — a rebuild would have nothing to reattach"

stamp_success full
log "DRILL PASSED — this snapshot restores and serves its objects"
log "stamped ${STAMP_DIR}/last-success-full; also record the date in docs/k3s-etcd-restore.md"
