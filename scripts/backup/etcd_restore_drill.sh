#!/usr/bin/env bash
#
# etcd_restore_drill.sh — prove an off-box etcd snapshot actually restores, without an outage.
#
# docs/k3s-etcd-restore.md documents a `--cluster-reset` on daniel-box. That is a real restore:
# it stops k3s, rolls the whole cluster back to the snapshot's moment, loses everything created
# since, and needs daniel-server's agent certs deleted so it can rejoin. Nobody was ever going
# to run that for practice, which is why the doc carried "procedure documented, NOT drilled"
# from the day it was written. An undrilled restore path is a restore path you
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
# graph a rebuild depends on. Since the drill moved into a throwaway guest it proves
# one thing more, and the guest is what makes it evidence: that host has no encryption key of
# its own, so the API server can only list Secrets by using the key the snapshot's own bootstrap
# blob carried — which is the path a rebuilt host takes. The drill still reports Secret COUNT
# (presence) and deliberately never decodes one. Run beside a live k3s it would prove less,
# because the local key would be in place already.
#
# The encryption key IS in the snapshot: the contents of encryption-config.json ride inside the
# snapshot's /bootstrap blob (verified against k3s v1.36.3+k3s1), encrypted with the cluster
# token. The artifact that has to survive daniel-box is therefore
# /var/lib/rancher/k3s/server/token, which this script depends on for exactly that reason
# (LIVE_TOKEN below, and the --cluster-reset note further down). docs/k3s-etcd-restore.md
# carries the evidence and the re-verify command.
#
# STATUS. The FULL drill passes in a throwaway guest. The endpoint-reconciler flag in
# ISOLATION_ARGS lets k3s v1.37.1 start the scratch server on its loopback advertise address.
# `--list-only` is the
# weekly cheap proof of the off-box leg on daniel-box. It also runs restore gate 3 against the
# snapshot it lists, so the runbook's ETCDSnapshotFile read is drilled weekly rather than on the
# day of an outage — see require_snapshot_restorable.
#
# It does NOT pass beside a live k3s, and that is structural rather than a bug here: `k3s server
# --cluster-reset` assumes it is the only k3s on the host, and every workaround found the next
# thing it assumes. This script handles each of these, from the v1.36.4 source. In order:
#
#   1. it needs <data-dir>/server/token to EXIST; --token-file does not satisfy it — and the
#      file is only a pre-check: the VALUE must arrive as --token or K3S_TOKEN, or k3s mints a
#      random one and overwrites the file (see the restore stage comment)
#   2. the reset stage starts its own listeners, so isolation flags belong on BOTH invocations
#   3. --disable-agent does not stop the supervisor client load-balancer on 127.0.0.1:6444;
#      --lb-server-port is the flag
#   4. --cluster-reset-restore-path is read twice: stat'd after k3s chdirs to <data-dir>/server,
#      then joined onto <data-dir>/server/db/snapshots for the .zip decompress — so an absolute
#      path doubles, a bare name alone "does not exist", and --etcd-s3 doubles it for you by
#      feeding its own download path back through the join. The bare name works with the file
#      hard-linked into both places
#   5. after all four, the run wedges in "Waiting to retrieve agent configuration; server is not
#      ready" — 17 minutes on 6 seconds of CPU, against ~60s when it resolves. This one is
#      not the live k3s: it reproduces in a guest with no k3s at all and is a port collision
#      inside this script's own isolation flags — see the port block.
#
# Item 5 cannot be diagnosed from daniel-box, because a live k3s explains it away. Throughout
# every failure the live cluster stays Ready and every write lands in /var/tmp.
#
# THE FULL DRILL RUNS IN A THROWAWAY GUEST. hosts/components/hypervisor
# installs `etcd-restore-drill-vm` on daniel-server: a monthly root cron that builds a transient
# libvirt guest from the reviewed cloud image, copies in the k3s binary, the cluster token and the
# R2 env file at the paths this script reads, runs THIS script there unmodified (through
# files/etcd-drill-guest-run.sh, which fetches the snapshot and hands it to --local-snapshot),
# pulls restore.log and server.log out as evidence, and destroys the guest. Its verdict is the
# etcd-restore-drill-vm textfile, which EtcdRestoreDrillFailed reads. The weekly --list-only cron
# on daniel-box is the cheap proof of the R2 leg on the host that owns the credentials.
#
# Usage:
#   sudo ./scripts/backup/etcd_restore_drill.sh --list-only      # the part that works: prove the R2 leg
#   sudo ./scripts/backup/etcd_restore_drill.sh                 # newest off-box snapshot in R2
#   sudo ./scripts/backup/etcd_restore_drill.sh --snapshot NAME # a specific one from the S3 listing
#   sudo ./scripts/backup/etcd_restore_drill.sh --keep          # leave the scratch dir for poking at
#   sudo ./scripts/backup/etcd_restore_drill.sh --clean         # remove every kept scratch dir, now
#
# A failed run always keeps its scratch dir — the logs are the evidence — and every run sweeps
# dirs older than 30 days on the way in (ETCD_DRILL_SCRATCH_RETENTION_DAYS to change it).
# `--clean` is the immediate form and needs no R2 credentials.
#
# Exit 0 = the snapshot restored and served objects. Non-zero = it did not, and the message
# says which stage. Run it after any change to the snapshot cron, and periodically regardless:
# the value is in the last time it passed, not in it existing.

set -euo pipefail

LIVE_DATA_DIR=/var/lib/rancher/k3s
# Read-only. `--cluster-reset` derives its encryption material from the cluster token, and the
# scratch data-dir has no `server/token`.
LIVE_TOKEN=/var/lib/rancher/k3s/server/token
S3_ENV=/etc/rancher/k3s/etcd-s3.env
SCRATCH="/var/tmp/etcd-restore-drill.$$"
# Four listeners from three flags. PORT is the supervisor and the only place the API server is
# served to clients; PORT+1 is the API server's internal listener; LB_PORT is the supervisor LB;
# LB_PORT-1 is the API-server LB. Do not use 7445 as LB_PORT, 7444 as SUPERVISOR_PORT, or a
# 7443/7445 split.
PORT=7443
SUPERVISOR_PORT=$PORT
LB_PORT=7448
KEEP=0
CLEAN_ONLY=0
SCRATCH_RETENTION_DAYS="${ETCD_DRILL_SCRATCH_RETENTION_DAYS:-30}"
LIST_ONLY=0
SNAPSHOT=""
LOCAL_SNAPSHOT=""
READY_TIMEOUT=180
# Two modes stamp separately and must not be conflated: `list-only` proves the off-box leg, `full`
# also proves the object graph.
STAMP_DIR="${ETCD_DRILL_STAMP_DIR:-/var/lib/etcd-restore-drill}"
# A run can wedge in k3s's "Waiting to retrieve agent configuration" loop, so the restore stage has
# its own bound (the loop takes ~60s when it resolves).
RESTORE_TIMEOUT=600

die() { echo "etcd-restore-drill: $*" >&2; exit 1; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# DECIDED: the stamp is read, and both halves are shipped. homelab-etcd-restore-drill.timer runs
# `--list-only` weekly (k3s_etcd_restore_drill_cron, Monday 10:20),
# run in place from the primary checkout. The fail-closed staleness
# reader is `check_etcd_restore_drill` in monitor-bridge/files/checks/service.py:
# it reads `last-success-list-only` from this STAMP_DIR, monitor-bridge serves the verdict as
# `homelab_check_success{check="etcd_restore_drill"}`, and the `EtcdRestoreDrillFailing` rule in
# observability's bridge-exporter.yaml pages on it.
#
# The ordering is the reason the pair works. A fail-closed reader wired BEFORE the cron
# exists sits red essentially always, which is a pager that trains the operator to ignore it;
# the cron is what makes a red mean something. monitor-bridge owns the reader because it
# already aggregates unrelated stamps — not longhorn-backup-health.sh, which would be reading
# a second drill's evidence out of the first drill's watchdog.
#
# The reader keys on the MODE. `last-success-list-only` and `last-success-full` are written
# separately and must not be conflated: only the weekly list-only leg is scheduled, so a reader
# that accepted either file as coverage would report the object-graph restore as proven when
# nothing has ever proven it on this host. The reader opens `last-success-list-only` by name and
# says so in its message.
#
# The stamp makes "when did this last pass" answerable at all.
#
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
  # 0644 explicitly: monitor-bridge reads this stamp as uid 1000, and the root cron's umask 027
  # would create 0640.
  chmod 0644 "${STAMP_DIR}/last-success-${mode}" 2>/dev/null || true
  return 0
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Gate 3 only: gate 1 reads this drill's own stamp, so running it here is circular. Any non-zero
# exit fails the drill, 69 included.
# Use the checkout's own `.venv/bin/python`: root has no uv-managed python, and the root cron must
# not write to the .venv.
GATE_SCRIPT="$REPO_ROOT/scripts/deploy_tools/runbook_gates.py"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  GATE_CMD=("$REPO_ROOT/.venv/bin/python" "$GATE_SCRIPT" etcd-restore --gate 3)
else
  GATE_CMD=(
    /usr/bin/env PYTHONPATH="$REPO_ROOT/scripts" /usr/bin/uv run --no-project --no-python-downloads
    --python "$(cat "$REPO_ROOT/.python-version" 2>/dev/null || echo 3.14)"
    "$GATE_SCRIPT" etcd-restore --gate 3
  )
fi

require_snapshot_restorable() {
  local snapshot="$1" rc=0
  log "restore gate 3: does the cluster call $snapshot restorable?"
  "${GATE_CMD[@]}" "$snapshot" || rc=$?
  [[ "$rc" == "0" ]] || die "restore gate 3 refused $snapshot (exit $rc) — the snapshot this drill listed is not one the cluster would restore, so this drill has not passed"
}

# The gate runs before the stamp, so a refusal leaves no `last-success-list-only` behind. Reads
# SNAPSHOT.
finish_list_only() {
  log "--list-only: the off-box leg works (credentials, bucket, folder, a named snapshot)"
  log "nothing was restored; re-run without --list-only for the actual drill"
  require_snapshot_restorable "$SNAPSHOT"
  stamp_success list-only
}

verify_restored_objects() {
  ns=$("${KUBECTL[@]}" get namespaces --no-headers 2>/dev/null | wc -l)
  deploys=$("${KUBECTL[@]}" get deployments -A --no-headers 2>/dev/null | wc -l)
  pvcs=$("${KUBECTL[@]}" get pvc -A --no-headers 2>/dev/null | wc -l)
  secrets=$("${KUBECTL[@]}" get secrets -A --no-headers 2>/dev/null | wc -l)
  crds=$("${KUBECTL[@]}" get crd --no-headers 2>/dev/null | wc -l)
  nodes=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}={.status.nodeInfo.kubeletVersion}{" "}{end}' 2>/dev/null)

  echo
  echo "restored from : ${SNAPSHOT:-unknown}"
  echo "nodes         : ${nodes:-unknown} (versions recorded in the snapshot)"
  echo "namespaces    : $ns"
  echo "deployments   : $deploys"
  echo "PVCs          : $pvcs"
  echo "CRDs          : $crds"
  echo "secrets       : $secrets (presence only — encrypted at rest, never decoded by this drill)"
  echo

  [[ "$ns" -ge 3 ]]      || die "only $ns namespaces — the snapshot restored but carries no cluster"
  [[ "$deploys" -ge 1 ]] || die "no deployments in the restored set"
  [[ "$pvcs" -ge 1 ]]    || die "no PVCs in the restored set — a rebuild would have nothing to reattach"
}

# Every isolation port and path goes to BOTH k3s invocations: `--cluster-reset` also brings up the
# apiserver LB and would bind the live 6444.
ISOLATION_ARGS=(--data-dir "$SCRATCH"
                --disable-agent
                --https-listen-port "$PORT"
                --supervisor-port "$SUPERVISOR_PORT"
                # A third listener: the supervisor client LB binds 127.0.0.1:6444 and
                # `--disable-agent` does not stop it.
                --lb-server-port "$LB_PORT"
                --bind-address 127.0.0.1
                --advertise-address 127.0.0.1
                # A loopback advertise address needs the endpoint reconciler off: the API server
                # refuses to start with one, and only `none` accepts any address.
                --kube-apiserver-arg=endpoint-reconciler-type=none
                # Needed on BOTH invocations: the live server encrypts Secrets at rest and the key
                # rides in the snapshot's bootstrap blob; without it the scratch API server cannot
                # list Secrets.
                --secrets-encryption
                # The restored APIServices point at nonexistent pod IPs; the default egress selector
                # would hang each call to a timeout.
                --egress-selector-mode=disabled)

# Sourced by the tests, so the array sits above this guard; everything below does real work.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT="${2:?--snapshot needs a name}"; shift 2 ;;
    # Restore from a local file: with --etcd-s3, k3s joins the absolute download path onto the
    # snapshots dir a second time and fails.
    --local-snapshot) LOCAL_SNAPSHOT="${2:?--local-snapshot needs a path}"; shift 2 ;;
    --keep)     KEEP=1; shift ;;
    --list-only) LIST_ONLY=1; shift ;;
    --clean)    CLEAN_ONLY=1; shift ;;
    -h|--help)  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "must run as root (k3s server needs it); use sudo"

# Runs before the R2/k3s preflight so cleanup works on a host where the drill cannot run. It ignores
# the age bound.
if [[ "$CLEAN_ONLY" == "1" ]]; then
  cleared=0
  for dir in /var/tmp/etcd-restore-drill.*; do
    [[ -d "$dir" ]] || continue
    log "removing $dir"
    rm -rf "$dir"
    cleared=$((cleared + 1))
  done
  log "cleared $cleared scratch dir(s)"
  exit 0
fi

[[ -r "$S3_ENV" ]] || die "cannot read $S3_ENV — this host does not hold the R2 credentials"
[[ -r "$LIVE_TOKEN" ]] || die "cannot read $LIVE_TOKEN — run this on the k3s server node"
command -v k3s >/dev/null || die "k3s not on PATH"

# The one irreversible step is opening the live data-dir with --cluster-reset.
case "$SCRATCH" in
  "$LIVE_DATA_DIR"*) die "refusing: scratch dir resolves inside the live data-dir" ;;
esac

# Swept here, not in cleanup(), so a run that dies before its trap still clears the previous mess.
sweep_old_scratch_dirs() {
  local found=0 dir
  for dir in /var/tmp/etcd-restore-drill.*; do
    # An unmatched glob expands to itself; -d rejects that as well as any stray file.
    [[ -d "$dir" ]] || continue
    [[ "$dir" == "$SCRATCH" ]] && continue
    [[ -n "$(find "$dir" -maxdepth 0 -mtime "+${SCRATCH_RETENTION_DAYS}" 2>/dev/null)" ]] || continue
    log "removing scratch dir older than ${SCRATCH_RETENTION_DAYS} days: $dir"
    rm -rf "$dir"
    found=$((found + 1))
  done
  [[ "$found" -gt 0 ]] && log "swept $found stale scratch dir(s)"
  return 0
}

sweep_old_scratch_dirs

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

cleanup() {
  local rc=$?
  # A failed drill keeps its scratch dir: its logs are the evidence and must not be removed on the
  # exit path.
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
  # `--cluster-reset-restore-path` is read twice and the reads disagree: the stat resolves relative
  # to <data-dir>/server, the .zip join doubles an absolute path.
  # Only the bare name works, with the file present at both places (snapshots dir and hard-linked
  # into <data-dir>/server).
  SNAPSHOT="$(basename "$LOCAL_SNAPSHOT")"
  RESTORE_S3_ARGS=()
  log "restoring from a local file, S3 not consulted"
elif [[ -z "$SNAPSHOT" ]]; then
  log "listing off-box snapshots"
  SNAPSHOT=$(k3s etcd-snapshot list "${S3_ARGS[@]}" 2>/dev/null \
             | awk 'NR>1 && $1 ~ /^offbox-/ {print $1}' | LC_ALL=C sort | tail -1)
  [[ -n "$SNAPSHOT" ]] || die "no offbox-* snapshot found in s3://${ETCD_S3_BUCKET}/etcd-snapshots"
fi
log "drilling snapshot: $SNAPSHOT"

if [[ "$LIST_ONLY" == "1" ]]; then
  finish_list_only
  exit 0
fi

# `install -d -m 700`: this holds the cluster token and a restored etcd database, and /var/tmp is
# world-traversable.
install -d -m 700 "$SCRATCH"
install -d -m 700 "$SCRATCH/server"
# The token must reach k3s two ways: the file seeds the `server/token` pre-check, but k3s takes the
# password from K3S_TOKEN and otherwise overwrites the file.
# The environment keeps it out of argv, where `ps` would show it.
install -m 600 "$LIVE_TOKEN" "$SCRATCH/server/token"
K3S_TOKEN="$(<"$LIVE_TOKEN")"
export K3S_TOKEN
if [[ -n "$LOCAL_SNAPSHOT" ]]; then
  install -D -m 600 "$LOCAL_SNAPSHOT" "$SCRATCH/server/db/snapshots/$SNAPSHOT"
  # The second place the bare name has to resolve — the comment at LOCAL_SNAPSHOT above.
  ln "$SCRATCH/server/db/snapshots/$SNAPSHOT" "$SCRATCH/server/$SNAPSHOT"
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
  if (( SECONDS >= deadline )); then
    { echo "== /readyz?verbose"; "${KUBECTL[@]}" get --raw '/readyz?verbose'
      echo "== listeners"; ss -ltnp 2>/dev/null | grep -E ':(7[0-9]{3}|2379) '
      echo "== server.log tail"; tail -20 "$SCRATCH/server.log"; } >&2 2>&1
    die "scratch server never became ready in ${READY_TIMEOUT}s"
  fi
  sleep 2
done
log "scratch API server is serving the restored objects"

verify_restored_objects

stamp_success full
log "DRILL PASSED — this snapshot restores and serves its objects"
log "stamped ${STAMP_DIR}/last-success-full; also record the date in docs/k3s-etcd-restore.md"
