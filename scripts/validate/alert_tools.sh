#!/usr/bin/env bash
# Provision promtool, amtool and pint, then run one of them.
#
# PROMTOOL AND AMTOOL FOLLOW THE IMAGE PINS. `observability_k8s_prometheus_image` and
# `observability_k8s_alertmanager_image` in the observability role's defaults decide which
# release is fetched. A Renovate image bump therefore moves the tool in the same PR, and the
# tool CI checks with is the one the pod runs.
#
# PINT FOLLOWS ITS OWN PIN. cloudflare/pint ships no image the cluster runs, so `pint_version`
# below is its version, and a Renovate regex manager in renovate.json bumps it.
#
# THE SUMS FILE DECIDES WHAT IS UNPACKED. Each tarball is checked against its release's sums
# file, `sha256sums.txt` for Prometheus's projects and `checksums.txt` for pint, before `tar`
# reads it. A digest missing from the sums file fails the same
# way as a wrong one, so a partial download or a renamed asset never reaches the cache.
#
# THE STAMP, NOT THE DIRECTORY, IS THE GUARD. A version directory holding files but no stamp
# is a half-finished unpack and is fetched again. The stamp is written only after a clean
# unpack, as `scripts/validate/vale.sh` does for its style package.
#
# THE CACHE IS IN THE GIT COMMON DIR, SO EVERY WORKTREE OF A CLONE SHARES IT. The tools live in
# `$(git rev-parse --git-common-dir)/alert-tools/`, which is `.git/alert-tools/` of the main
# checkout. A cache inside each checkout made every fresh worktree download about 150 MB on its
# first alert-plane commit, 6m36s on daniel-box on 2026-10-12, and fail that commit offline
# (#4705). Each user on a host has its own clone, so one uid owns the cache. Outside a git
# checkout the script fails rather than fall back to a per-checkout cache.
#
# THE LOCK IS ON THE alert-tools/ DIRECTORY, NOT ON A VERSION DIRECTORY. The critical section
# removes the version directory, so a lock held on it would leave every waiter holding a lock
# on a deleted inode. The parent is opened read-only on fd 9, so no lock file is created. The
# lock and the stamp together make concurrent provisions from several worktrees safe.
#
# With no arguments the script provisions all three tools and exits 0. That is how CI
# provisions before the hooks run. With `promtool ...`, `amtool ...` or `pint ...` it
# provisions, then runs that tool with the remaining arguments.
#
# Run: scripts/validate/alert_tools.sh [promtool|amtool|pint [arg ...]]
set -euo pipefail

# `--help` answers before any fetch. The awk prints this file's leading comment block, which
# is the usage, with the `#` markers stripped.
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
defaults="$repo/ansible/roles/k8s/observability/defaults/main.yml"
# --path-format=absolute because a main checkout's common dir is otherwise the relative `.git`.
# Under a git hook GIT_DIR overrides -C, and it names a directory of this same clone.
common_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir)"
cache="$common_dir/alert-tools"
# The fetch directory of the provision in progress. The trap removes it on any exit, so a
# failed curl or a refused digest leaves nothing behind under the cache.
tmp=""
trap 'if [[ -n "$tmp" ]]; then rm -rf "$tmp"; fi' EXIT

case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *)
    echo "alert_tools.sh: no release asset for architecture $(uname -m)" >&2
    exit 1
    ;;
esac

#######################################
# Print the release version an image pin in the observability defaults names.
# Globals:
#   defaults
# Arguments:
#   The defaults key, such as observability_k8s_prometheus_image.
# Outputs:
#   The version without its leading `v` on STDOUT; an error on STDERR.
#######################################
image_version() {
  local line version
  line="$(grep -m1 -E "^$1:" "$defaults" || true)"
  version="$(printf '%s\n' "$line" | sed -nE 's/^[^:]+:[^:]+:v([0-9]+\.[0-9]+\.[0-9]+)(@.*)?$/\1/p')"
  if [[ -z "$version" ]]; then
    echo "alert_tools.sh: no vX.Y.Z tag on '$1' in $defaults" >&2
    return 1
  fi
  printf '%s\n' "$version"
}

#######################################
# Fetch, verify and unpack one tool unless its stamped version directory already exists.
# Globals:
#   cache, tmp (set while a fetch is in progress)
# Arguments:
#   The cache name (prometheus, alertmanager or pint), its version, the release download URL,
#   the tarball name without `.tar.gz`, the sums file's name, the binary's path inside the
#   tarball, and the name to install it under.
# Outputs:
#   Errors on STDERR.
#######################################
provision() {
  local name="$1" version="$2" url="$3" base="$4" sums="$5" member="$6" binary="$7"
  local dir="$cache/$name-$version"
  local stamp="$dir/.provisioned"
  local want got

  if [[ "$(cat "$stamp" 2>/dev/null || true)" == "$base" ]]; then
    return 0
  fi
  mkdir -p "$cache"
  exec 9<"$cache"
  # -w rather than an indefinite block: a wedged commit with no output is worse than a loud
  # failure. The holder fetches one tarball, 112 MB for Prometheus v3.15.0, and that fetch
  # took about 6 minutes on daniel-box on 2026-10-12, so the bound sits well above it.
  if ! flock -w 1200 9; then
    echo "alert_tools.sh: timed out waiting for the $cache lock" >&2
    return 1
  fi
  # Re-read under the lock: a waiter that queued behind the winner finds the tool fetched.
  if [[ "$(cat "$stamp" 2>/dev/null || true)" != "$base" ]]; then
    rm -rf "$dir"
    tmp="$(mktemp -d "$cache/.fetch.XXXXXX")"
    # The speed bound aborts a connection that stalls below 1 KB/s for a minute. Without it a
    # hung fetch blocks a local commit with no output and no end.
    curl -fsSL --speed-limit 1024 --speed-time 60 -o "$tmp/$base.tar.gz" "$url/$base.tar.gz"
    curl -fsSL --speed-limit 1024 --speed-time 60 -o "$tmp/$sums" "$url/$sums"
    want="$(awk -v f="$base.tar.gz" '$2 == f {print $1}' "$tmp/$sums")"
    got="$(sha256sum "$tmp/$base.tar.gz" | cut -d' ' -f1)"
    if [[ -z "$want" || "$want" != "$got" ]]; then
      echo "alert_tools.sh: $base.tar.gz has sha256 $got, and $sums lists '${want:-nothing}'" >&2
      return 1
    fi
    # Prometheus's tarballs hold `<base>/promtool`, and pint's holds `pint-linux-<arch>` at
    # its root, so the member is unpacked into the fetch directory and installed by name.
    tar xzf "$tmp/$base.tar.gz" -C "$tmp" "$member"
    mkdir -p "$dir"
    install -m 0755 "$tmp/$member" "$dir/$binary"
    rm -rf "$tmp"
    tmp=""
    printf '%s\n' "$base" >"$stamp"
  fi
  exec 9<&-
}

# Renovate's cloudflare/pint manager rewrites this line; keep it a literal.
pint_version="0.89.2"
prometheus_version="$(image_version observability_k8s_prometheus_image)"
alertmanager_version="$(image_version observability_k8s_alertmanager_image)"
prometheus_base="prometheus-$prometheus_version.linux-$arch"
alertmanager_base="alertmanager-$alertmanager_version.linux-$arch"
provision prometheus "$prometheus_version" \
  "https://github.com/prometheus/prometheus/releases/download/v$prometheus_version" \
  "$prometheus_base" sha256sums.txt "$prometheus_base/promtool" promtool
provision alertmanager "$alertmanager_version" \
  "https://github.com/prometheus/alertmanager/releases/download/v$alertmanager_version" \
  "$alertmanager_base" sha256sums.txt "$alertmanager_base/amtool" amtool
provision pint "$pint_version" \
  "https://github.com/cloudflare/pint/releases/download/v$pint_version" \
  "pint-$pint_version-linux-$arch" checksums.txt "pint-linux-$arch" pint

case "${1:-}" in
  "") exit 0 ;;
  promtool) tool="$cache/prometheus-$prometheus_version/promtool" ;;
  amtool) tool="$cache/alertmanager-$alertmanager_version/amtool" ;;
  pint) tool="$cache/pint-$pint_version/pint" ;;
  *)
    echo "alert_tools.sh: unknown tool '$1'; expected promtool, amtool or pint" >&2
    exit 2
    ;;
esac
shift
exec "$tool" "$@"
