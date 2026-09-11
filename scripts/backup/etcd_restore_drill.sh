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
# graph a rebuild depends on. Since the drill moved into a throwaway guest (#1175) it proves
# one thing more, and the guest is what makes it evidence: that host has no encryption key of
# its own, so the API server can only list Secrets by using the key the snapshot's own bootstrap
# blob carried — which is the path a rebuilt host takes. The drill still reports Secret COUNT
# (presence) and deliberately never decodes one. Run beside a live k3s it would prove less,
# because the local key would be in place already.
#
# Corrected 2026-08-23: this comment used to say the key "is not in the snapshot". It is — the
# contents of encryption-config.json ride inside the snapshot's /bootstrap blob (verified against
# k3s v1.36.3+k3s1), encrypted with the cluster token. The artifact that has to survive daniel-box
# is therefore /var/lib/rancher/k3s/server/token, which this script already depends on for exactly
# that reason (LIVE_TOKEN below, and the --cluster-reset note further down). An out-of-band copy
# was taken 2026-08-23; docs/k3s-etcd-restore.md carries the evidence and the re-verify command.
#
# STATUS, 2026-09-11. The FULL drill passes, in a throwaway guest: offbox-daniel-box-1789094702.zip
# restored and served 8 namespaces, 72 Deployments, 45 PVCs, 48 CRDs and 51 Secrets, 50 seconds
# end to end. `--list-only` works too and remains the weekly cheap proof of the off-box leg on
# daniel-box (first proven 2026-08-22 with offbox-daniel-box-1787366702.zip).
#
# It does NOT pass beside a live k3s, and that is structural rather than a bug here: `k3s server
# --cluster-reset` assumes it is the only k3s on the host, and every workaround found the next
# thing it assumes. Items 1-4 were fixed on 2026-08-22 and items 1, 4 and 5 corrected on
# 2026-09-11 from the v1.36.4 source once the guest could reproduce them cleanly. In order:
#
#   1. it needs <data-dir>/server/token to EXIST; --token-file does not satisfy it — and the
#      file is only a pre-check: the VALUE must arrive as --token or K3S_TOKEN, or k3s mints a
#      random one and overwrites the file (corrected 2026-09-11, restore stage comment)
#   2. the reset stage starts its own listeners, so isolation flags belong on BOTH invocations
#   3. --disable-agent does not stop the supervisor client load-balancer on 127.0.0.1:6444;
#      --lb-server-port is the flag
#   4. --cluster-reset-restore-path is read twice: stat'd after k3s chdirs to <data-dir>/server,
#      then joined onto <data-dir>/server/db/snapshots for the .zip decompress — so an absolute
#      path doubles, a bare name alone "does not exist", and --etcd-s3 doubles it for you by
#      feeding its own download path back through the join. The bare name works with the file
#      hard-linked into both places (corrected 2026-09-11 from the k3s source; this item used
#      to say "a name relative to the snapshots dir")
#   5. after all four, the run wedges in "Waiting to retrieve agent configuration; server is not
#      ready" — 17 minutes on 6 seconds of CPU, against ~60s when it resolves. CORRECTED
#      2026-09-11: this one was never the live k3s. It reproduced in a guest with no k3s at all
#      and is a port collision inside this script's own isolation flags — see the port block.
#
# Item 5 was the only one that could not be diagnosed from daniel-box, because a live k3s
# explained it away; in the guest it turned out to be a collision among this script's OWN
# listeners. Throughout every failure the live cluster stayed Ready and every write landed in
# /var/tmp — the isolation held, which is what made all of this safe to iterate on.
#
# THE FULL DRILL RUNS IN A THROWAWAY GUEST since issue #1175 (path 1). roles/setup/hypervisor
# installs `etcd-restore-drill-vm` on daniel-server: a monthly root cron that builds a transient
# libvirt guest from the reviewed cloud image, copies in the k3s binary, the cluster token and the
# R2 env file at the paths this script reads, runs THIS script there unmodified (through
# files/etcd-drill-guest-run.sh, which fetches the snapshot and hands it to --local-snapshot),
# pulls restore.log and server.log out as evidence, and destroys the guest. Its verdict lands on
# the `etcd Restore Drill (full)` Kuma tile. The weekly --list-only cron on daniel-box is
# unchanged and stays the cheap proof of the R2 leg on the host that owns the credentials.
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
# Read-only, and the only thing this drill takes from the live installation. `--cluster-reset`
# derives the restore's encryption material from the cluster token, and a scratch data-dir has
# no `server/token` of its own — the first run failed with exactly that. The token reaches k3s
# through the K3S_TOKEN environment variable (the restore stage below says why the file alone
# is not enough), so it never lands in argv or in this script's output.
LIVE_TOKEN=/var/lib/rancher/k3s/server/token
S3_ENV=/etc/rancher/k3s/etcd-s3.env
SCRATCH="/var/tmp/etcd-restore-drill.$$"
# Four listeners from three flags, so the layout is not free (v1.36.4 source, 2026-09-11):
#   PORT              the supervisor, which also proxies the API server — the kubeconfig k3s
#                     writes points here, and this is the ONLY place the API server is served
#                     to clients: with --supervisor-port set to a different value nothing binds
#                     PORT at all ("connection refused", measured in the throwaway guest)
#   PORT + 1          the API server's internal listener (control/server.go, APIServerPort)
#   LB_PORT           the supervisor client load-balancer
#   LB_PORT - 1       the API-server client load-balancer, bound only when the supervisor and
#                     the API server are on different ports; kept free anyway
# So the supervisor stays colocated with the API server, exactly as a stock k3s runs, and the
# isolation from the live 6443/6444 comes from the values alone. Three collisions were measured
# before this was written down: 7445 as LB_PORT put the API-server LB on the supervisor's 7444
# (header item 5, the agent-config loop); 7444 as SUPERVISOR_PORT collided with the API server's
# own 7444 when the scratch server started ("bind: address already in use"); and a split
# 7443/7445 left 7443 unbound. The test in ansible/tests/setup pins the layout.
PORT=7443
SUPERVISOR_PORT=$PORT
LB_PORT=7448
KEEP=0
CLEAN_ONLY=0
# How long a kept scratch dir survives. Only failed runs and --keep runs leave one, so these are
# evidence rather than litter — long enough to still be there when someone gets round to reading
# it, short enough that a forgotten failure does not hold a restored etcd database indefinitely.
SCRATCH_RETENTION_DAYS="${ETCD_DRILL_SCRATCH_RETENTION_DAYS:-30}"
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

# DECIDED: the stamp is read, and both halves are shipped. health-crons.yml schedules
# `--list-only` weekly under the `etcd-snapshot` tag (k3s_etcd_restore_drill_cron, Monday 10:20),
# run in place from the primary checkout, since 2026-08-28 (PR #531). The fail-closed staleness
# reader is `check_etcd_restore_drill` in monitor-bridge/check.py, since PR #535: it reads
# `last-success-list-only` from this STAMP_DIR and pushes the `etcd Restore Drill` Kuma monitor.
#
# The ordering this block fixed is kept rather than deleted, because it is the reason the pair
# works. A fail-closed reader wired BEFORE the cron existed would have sat red essentially
# always, which is a pager that trains the operator to ignore it; the cron is what makes a red
# mean something. monitor-bridge/check.py is the owner because it already aggregates unrelated
# stamps — not longhorn-backup-health.sh, which would be reading a second drill's evidence out of
# the first drill's watchdog (2026-08-23b review M7).
#
# The reader keys on the MODE. `last-success-list-only` and `last-success-full` are written
# separately and must not be conflated: only the weekly list-only leg is scheduled, so a reader
# that accepted either file as coverage would report the object-graph restore as proven when
# nothing has ever proven it on this host. The reader opens `last-success-list-only` by name and
# says so in its message.
#
# The stamp still earns its place unread: it is what makes "when did this last pass" answerable
# at all, where before 2026-08-23 the only record was a hand-edited line in
# docs/k3s-etcd-restore.md.
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
  # 0644 EXPLICITLY, because the reader is not root. This runs as a root cron under
  # /etc/login.defs UMASK 027, so the redirect above creates 0640 root:root — and the first real
  # run on 2026-08-28 did exactly that. monitor-bridge reads this stamp as uid 1000
  # (runAsNonRoot), where an unreadable file and an absent one are indistinguishable: both would
  # report "the drill has never passed" while it passes weekly. The Longhorn drill beside this
  # one already chmods its stamps for the same reason.
  chmod 0644 "${STAMP_DIR}/last-success-${mode}" 2>/dev/null || true
  return 0
}

# The verification stage, pulled out so it can be exercised without a real restore: a fixture
# `kubectl` returning zero namespaces/deployments/PVCs must drive each `die` below, which is the
# proof issue #1017 asked for (CLAUDE.md: "a new check ships with a proof it can go RED"). Reads
# the KUBECTL array and SNAPSHOT the caller has already set; has no side effect beyond echo/die,
# so sourcing this file and calling it is enough — see
# ansible/tests/setup/test_etcd_restore_drill_verify.py.
verify_restored_objects() {
  # Counts, not names: the point is that the object graph came back, and a count that reads
  # zero is the failure this drill exists to catch.
  ns=$("${KUBECTL[@]}" get namespaces --no-headers 2>/dev/null | wc -l)
  deploys=$("${KUBECTL[@]}" get deployments -A --no-headers 2>/dev/null | wc -l)
  pvcs=$("${KUBECTL[@]}" get pvc -A --no-headers 2>/dev/null | wc -l)
  secrets=$("${KUBECTL[@]}" get secrets -A --no-headers 2>/dev/null | wc -l)
  crds=$("${KUBECTL[@]}" get crd --no-headers 2>/dev/null | wc -l)
  # The Node objects ride in the snapshot with the kubelet version that wrote it, which is the
  # only record of the writer's k3s version a snapshot carries. Informational: a restore across
  # versions is k3s's supported upgrade path, but the "same binary that wrote it" argument in the
  # header only holds while this line and the binary agree, so the guest drill prints both.
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

  # A restore that yields an empty or near-empty object set is the silent failure worth
  # catching: the stages all "succeed" and the cluster comes up carrying nothing.
  [[ "$ns" -ge 3 ]]      || die "only $ns namespaces — the snapshot restored but carries no cluster"
  [[ "$deploys" -ge 1 ]] || die "no deployments in the restored set"
  [[ "$pvcs" -ge 1 ]]    || die "no PVCs in the restored set — a rebuild would have nothing to reattach"
}

# Sourced by the test above to drive verify_restored_objects() in isolation. Everything from here
# down does real things — argument parsing, the root check, reading live S3 credentials — so a
# sourced invocation must stop before any of it runs.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return 0
fi

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
    --clean)    CLEAN_ONLY=1; shift ;;
    # Prints the comment header, however long it grows — a hardcoded line range silently
    # truncates the moment anything is added above.
    -h|--help)  awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "must run as root (k3s server needs it); use sudo"

# Before the R2/k3s preflight below, deliberately: clearing scratch dirs is useful precisely on a
# host where the drill cannot run, and gating it on credentials it does not use would make the
# cleanup unavailable exactly when someone wants it. This is the reverse of --keep — a flag that
# creates state needs a matching way to remove it. It ignores the age bound, since an operator
# asking for it has finished reading the logs.
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

# The one guard that matters. Everything else in this script is reversible; opening the live
# data-dir with --cluster-reset is not.
case "$SCRATCH" in
  "$LIVE_DATA_DIR"*) die "refusing: scratch dir resolves inside the live data-dir" ;;
esac

# Kept scratch dirs have no expiry anywhere else. cleanup() sets KEEP=1 on ANY non-zero exit, so
# every failed drill leaves a restored etcd database plus a cluster-token copy behind — and the
# 2026-08-22 session alone produced five failures. Nothing sweeps them: /var/tmp survives
# reboots on the root ext4 LV, and systemd-tmpfiles ships its /var/tmp age rule COMMENTED OUT on
# this host (`systemd-tmpfiles --cat-config` shows only systemd-private-* lines). The only
# scheduled arm is `--list-only`, which returns above the `install -d "$SCRATCH"` below and so
# creates no dir to keep — a weekly failing cron accumulates nothing. Accumulation is therefore
# still operator-paced, as the five dirs above were: they came from hand runs, and were removed by
# hand (2026-08-23b review M8). That bound holds only while the full drill stays unscheduled ON
# THIS HOST: the scheduled full drill (header) runs in a guest whose whole disk is deleted after
# every run, so its --keep dirs never reach any host's /var/tmp.
#
# Swept here rather than in cleanup(): a run that dies before its trap installs still gets the
# previous mess cleared, and this way the sweep is exercised on every invocation instead of only
# the successful ones.
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
                --advertise-address 127.0.0.1
                # The live server encrypts Secrets at rest (k3s_secrets_encryption), and the
                # key rides inside the snapshot's bootstrap blob. Without this flag the restore
                # stage's reconcile has no path to write it to ("Unable to lookup path to
                # reconcile EncryptionConfig") and the scratch API server then cannot list a
                # single Secret ("identity transformer tried to read encrypted data"), which
                # holds its informer-sync readiness check open until the deadline — measured in
                # the guest 2026-09-11. On BOTH invocations, because the restore writes the file
                # the server reads.
                --secrets-encryption
                # The restored cluster's aggregated APIServices point at pod IPs that do not
                # exist here; with the default egress selector the API server dials them through
                # the agent tunnel and each call hangs to a timeout (dial tcp 10.42.x.x:10250).
                --egress-selector-mode=disabled)

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
  # `--cluster-reset-restore-path` is read TWICE by k3s, and the two reads disagree (read in
  # v1.36.4's pkg/etcd/etcd.go and pkg/server/server.go, 2026-09-11). k3s chdirs to
  # <data-dir>/server first (setupDataDirAndChdir), then stats the value AS GIVEN — so a
  # relative name resolves under <data-dir>/server, whatever the caller's cwd was. The .zip
  # decompress then does filepath.Join(<data-dir>/server/db/snapshots, value), so an absolute
  # path passes the stat and doubles at the join, measured twice on 2026-08-22:
  #   open <scratch-A>/server/db/snapshots/<scratch-B>/server/db/snapshots/<name>.zip
  # No single path satisfies both reads. The bare name does, with the file present at BOTH
  # places — the restore stage below stages it into the snapshots dir and hard-links it into
  # <data-dir>/server. The same join is why --etcd-s3 fails here: k3s downloads to the
  # snapshots dir, then feeds the absolute download path back through it.
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

# `install -d -m 700`, not `mkdir -p`. This holds a copy of the cluster token and a restored
# etcd database, and /var/tmp is world-traversable. It is NOT currently exposed: /etc/login.defs
# UMASK 027, applied to sudo sessions via pam_umask.so, means a bare `mkdir -p` here yields 0750
# and "other" is already blocked (2026-08-23b review M8, whose stated 0755 mechanism was refuted
# on exactly this point). The explicit mode is defence against that umask changing under the
# script, which nothing here would notice.
install -d -m 700 "$SCRATCH"
install -d -m 700 "$SCRATCH/server"
# The token has to reach k3s TWO ways, and the file alone is a trap. `--cluster-reset` refuses
# unless <data-dir>/server/token EXISTS ("server/token does not exist, please pass --token",
# measured twice 2026-08-22) — but that is only a pre-check in pkg/cli/server; nothing reads
# the file into the config. The server password comes from config.Token (the --token flag or
# K3S_TOKEN) and is otherwise a fresh random one (deps.go getServerPass), which printTokens
# then WRITES OVER the staged file. The restore succeeded and died one step later on
# "bootstrap data already found and encrypted with different token" (guest run 2026-09-11,
# read in the v1.36.4 source). So: seed the file for the pre-check, and export K3S_TOKEN for
# the value. The environment keeps it out of argv, where `--token <value>` would expose it to
# any `ps` on this host; /proc/<pid>/environ is root-only.
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
    # The answer to "ready for what?" is in the check's own body, which a silent loop throws
    # away; the listener table says whether kubectl reached the server at all.
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
