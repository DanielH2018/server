#!/bin/sh
# Whether this node's ghcr.io mirror endpoint serves nut's manifest, written as a node_exporter
# textfile check file (#5542).
#
# containerd tries the registry role's ghcr-mirror first and then ghcr.io itself, so a mirror
# that a NetworkPolicy grant, a bad endpoint or a crash loop has broken stays invisible while
# the WAN is up. The first thing to notice would be nut's cold start in a WAN outage, the case
# the mirror exists for. This check GETs nut's manifest through the same endpoint containerd
# names in registries.yaml, and from the host network namespace, so the request arrives at the
# mirror's fence SNAT'd to the node grant exactly as containerd's pulls do.
#
# It runs as the `ghcr-mirror-pull` sidecar of the node-exporter DaemonSet, which is
# hostNetwork, and whose template inlines this file. nut's role renders the tag to GET into the
# `nut-ghcr-mirror-probe` ConfigMap, because only nut's render sees nut's content tag. The
# sidecar mounts it as a directory and reads it on every write, so a new tag reaches the check
# within the kubelet's ConfigMap sync without a roll. A missing file is a down verdict, never a
# skipped write. The file follows the check-file half of M3's producer metric contract
# (docs/target-architecture/monitoring/m3-textfile-timer.md).
#
# The `dockerhub-mirror-pull` sidecar runs this same script against the docker.io mirror
# (#6535). It sets CHECK, and names a fixed REPOSITORY and REFERENCE in place of TARGET_DIR's
# files: the exporter's own image by digest. A digest GET is served from the mirror's store once
# cached, so the check spends none of Docker Hub's anonymous pull limit, where a tag GET would.
#
# Environment:
#   NODE_NAME       this node's name (the downward API's spec.nodeName)
#   REGISTRY_NODE   the node that runs the mirror, which reaches it on the loopback hostPort
#   LOCAL_ENDPOINT  the mirror endpoint on REGISTRY_NODE
#   REMOTE_ENDPOINT the mirror endpoint on every other node, the pinned ClusterIP
# Optional: CHECK (the check's name and file name, default ghcr-mirror-pull), REPOSITORY and
# REFERENCE (a tag or digest; when REPOSITORY is set, TARGET_DIR is not read), TARGET_DIR
# (default /ghcr-mirror-probe), TEXTFILE_DIR (default /ghcr-mirror-textfile), INTERVAL_S
# (default 600), TIMEOUT_S (default 30) and ONCE (set to 1 to write one file and exit, for the
# test).
# POSIX sh, because the image's shell is busybox ash, and its wget is busybox's.
set -u

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

TARGET_DIR=${TARGET_DIR:-/ghcr-mirror-probe}
TEXTFILE_DIR=${TEXTFILE_DIR:-/ghcr-mirror-textfile}
INTERVAL_S=${INTERVAL_S:-600}
TIMEOUT_S=${TIMEOUT_S:-30}
ONCE=${ONCE:-0}
CHECK=${CHECK:-ghcr-mirror-pull}
# The longest legitimate gap between two writes: five intervals. The GhcrMirrorPullNotReporting
# and DockerhubMirrorPullNotReporting rules in observability's registry.yaml carry it as a
# literal, and this role's test_ghcr_mirror_pull.py and test_dockerhub_mirror_pull.py pin them.
DEADLINE_S=$((INTERVAL_S * 5))
# Every manifest type a registry can store, so a served reference never answers 404 for its type.
ACCEPT=application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json

if [ "${NODE_NAME:-}" = "${REGISTRY_NODE:-}" ]; then
  ENDPOINT=${LOCAL_ENDPOINT:-}
else
  ENDPOINT=${REMOTE_ENDPOINT:-}
fi

# One verdict: 1 when the GET answered 2xx, 0 otherwise. Each down names its reason on stderr,
# which is the container log the alert's description points at.
verdict() {
  if [ -n "${REPOSITORY:-}" ]; then
    repository=$REPOSITORY
    tag=${REFERENCE:-}
  else
    repository=$(head -n 1 "$TARGET_DIR/repository" 2>/dev/null) || repository=
    tag=$(head -n 1 "$TARGET_DIR/tag" 2>/dev/null) || tag=
  fi
  if [ -z "$ENDPOINT" ]; then
    echo "$CHECK: no mirror endpoint for node ${NODE_NAME:-unset}" >&2
    echo 0
    return
  fi
  if [ -z "$repository" ] || [ -z "$tag" ]; then
    echo "$CHECK: no repository and reference, from the environment or $TARGET_DIR, so there is nothing to pull" >&2
    echo 0
    return
  fi
  url="$ENDPOINT/v2/$repository/manifests/$tag"
  # busybox wget exits non-zero on a refused connect, a timeout and any status other than 2xx.
  if error=$(wget -q -T "$TIMEOUT_S" -O /dev/null --header "Accept: $ACCEPT" "$url" 2>&1); then
    echo 1
  else
    echo "$CHECK: GET $url failed: $error" >&2
    echo 0
  fi
}

write_once() {
  tmp=$(mktemp "$TEXTFILE_DIR/$CHECK.prom.XXXXXX") || {
    echo "$CHECK: cannot create a temp file in $TEXTFILE_DIR" >&2
    return 1
  }
  success=$(verdict)
  now=$(date +%s)
  {
    # The homelab_check_* HELP lines must match disk-health.sh's byte for byte. node_exporter
    # drops a family whose HELP differs from an earlier file's and sets
    # node_textfile_scrape_error, so a mismatch loses these series on the node.
    printf "# HELP homelab_check_success 1 when the check's last verdict was up, 0 when down.\n"
    printf '# TYPE homelab_check_success gauge\n'
    printf 'homelab_check_success{check="%s"} %s\n' "$CHECK" "$success"
    printf "# HELP homelab_check_last_run_timestamp_seconds Unix time the check's last verdict was taken.\n"
    printf '# TYPE homelab_check_last_run_timestamp_seconds gauge\n'
    printf 'homelab_check_last_run_timestamp_seconds{check="%s"} %s\n' "$CHECK" "$now"
    printf "# HELP homelab_check_deadline_seconds Longest legitimate gap between two of the check's writes.\n"
    printf '# TYPE homelab_check_deadline_seconds gauge\n'
    printf 'homelab_check_deadline_seconds{check="%s"} %s\n' "$CHECK" "$DEADLINE_S"
  } > "$tmp"
  # mktemp creates 0600; the exporter in the other container reads as the same uid, and 0644
  # keeps the contract's mode for any other reader.
  if ! { chmod 0644 "$tmp" && mv "$tmp" "$TEXTFILE_DIR/$CHECK.prom"; }; then
    rm -f "$tmp"
    echo "$CHECK: cannot replace $TEXTFILE_DIR/$CHECK.prom" >&2
    return 1
  fi
}

while :; do
  write_once || true
  [ "$ONCE" = 1 ] && exit 0
  sleep "$INTERVAL_S"
done
