#!/usr/bin/env bash
# Provision promtool and amtool at the observability role's image tags, then run one of them.
#
# THE VERSIONS COME FROM THE IMAGE PINS. `observability_k8s_prometheus_image` and
# `observability_k8s_alertmanager_image` in the observability role's defaults decide which
# release is fetched. A Renovate image bump therefore moves the tool in the same PR, and the
# tool CI checks with is the one the pod runs.
#
# THE SUMS FILE DECIDES WHAT IS UNPACKED. Each tarball is checked against its release's
# `sha256sums.txt` before `tar` reads it. A digest missing from the sums file fails the same
# way as a wrong one, so a partial download or a renamed asset never reaches the cache.
#
# THE STAMP, NOT THE DIRECTORY, IS THE GUARD. A version directory holding files but no stamp
# is a half-finished unpack and is fetched again. The stamp is written only after a clean
# unpack, as `scripts/validate/vale.sh` does for its style package.
#
# THE LOCK IS ON .cache/alert-tools/, NOT ON A VERSION DIRECTORY. The critical section removes
# the version directory, so a lock held on it would leave every waiter holding a lock on a
# deleted inode. The parent is opened read-only on fd 9, so no lock file is created.
#
# .cache/ is untracked and ignored by `.gitignore`'s root `/*` rule, and
# `scripts/validate/root_ignored_files.py` lists it in LOCAL_ONLY so its hook stays quiet.
#
# With no arguments the script provisions both tools and exits 0. That is how CI provisions
# before the hooks run. With `promtool ...` or `amtool ...` it provisions, then runs that tool
# with the remaining arguments.
#
# Run: scripts/validate/alert_tools.sh [promtool|amtool [arg ...]]
set -euo pipefail

# `--help` answers before any fetch. The awk prints this file's leading comment block, which
# is the usage, with the `#` markers stripped.
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
defaults="$repo/ansible/roles/k8s/observability/defaults/main.yml"
cache="$repo/.cache/alert-tools"
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
#   arch, cache, tmp (set while a fetch is in progress)
# Arguments:
#   The upstream project (prometheus or alertmanager), its version, and the binary to keep.
# Outputs:
#   Errors on STDERR.
#######################################
provision() {
  local project="$1" version="$2" binary="$3"
  local dir="$cache/$project-$version"
  local stamp="$dir/.provisioned"
  local base="$project-$version.linux-$arch"
  local url="https://github.com/prometheus/$project/releases/download/v$version"
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
    curl -fsSL --speed-limit 1024 --speed-time 60 -o "$tmp/sha256sums.txt" "$url/sha256sums.txt"
    want="$(awk -v f="$base.tar.gz" '$2 == f {print $1}' "$tmp/sha256sums.txt")"
    got="$(sha256sum "$tmp/$base.tar.gz" | cut -d' ' -f1)"
    if [[ -z "$want" || "$want" != "$got" ]]; then
      echo "alert_tools.sh: $base.tar.gz has sha256 $got, and sha256sums.txt lists '${want:-nothing}'" >&2
      return 1
    fi
    mkdir -p "$dir"
    tar xzf "$tmp/$base.tar.gz" -C "$dir" --strip-components=1 "$base/$binary"
    rm -rf "$tmp"
    tmp=""
    printf '%s\n' "$base" >"$stamp"
  fi
  exec 9<&-
}

prometheus_version="$(image_version observability_k8s_prometheus_image)"
alertmanager_version="$(image_version observability_k8s_alertmanager_image)"
provision prometheus "$prometheus_version" promtool
provision alertmanager "$alertmanager_version" amtool

case "${1:-}" in
  "") exit 0 ;;
  promtool) tool="$cache/prometheus-$prometheus_version/promtool" ;;
  amtool) tool="$cache/alertmanager-$alertmanager_version/amtool" ;;
  *)
    echo "alert_tools.sh: unknown tool '$1'; expected promtool or amtool" >&2
    exit 2
    ;;
esac
shift
exec "$tool" "$@"
