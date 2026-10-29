#!/bin/sh
# Prune the ghcr-mirror store offline — managed by Ansible (k8s/registry role); edits overwritten.
#
# The ghcr-mirror container is a pull-through cache with REGISTRY_PROXY_TTL=0s, so nothing
# expires what it cached, and a registry in proxy mode refuses manifest DELETE (#5541). The
# dockerhub-mirror's GC Job runs this same script over that mirror's store (#6536). This
# script edits the store's filesystem directly while registry-gc.sh holds the registry pod at
# zero replicas. The Job's second container then runs plain `garbage-collect`, which sweeps the
# blobs no remaining revision references.
#
# THE UNIT IS A MANIFEST REVISION, NOT A TAG. A pull caches two shapes here. A third-party
# ghcr.io image pinned `name:tag@sha256:...` is pulled by digest, and the proxy stores a revision
# link with no tag beside it. A flux-owned built image is rewritten to `ghcr.io/danielh2018/
# <name>:<tag>` with no digest (scripts/renderer/renderer_lib/images.py:ghcr_text), so it is
# pulled by tag and cached tagged. A tag-based prune would never reclaim the first shape, and
# `garbage-collect --delete-untagged` would delete all of it. That is why the Job's
# garbage-collect runs without that flag.
#
# Per repository it keeps:
#   - the KEEP_NEWEST newest top-level revisions, ranked by the mtime of their revision link,
#     which is the time the mirror first cached them;
#   - every revision whose digest is listed in KEEP_FILE. registry-gc.sh writes there every
#     digest a pod resolved or a workload pins, in every namespace;
#   - every revision a tag listed in KEEP_FILE as `<repo>:<tag>` points at. registry-gc.sh
#     writes there every tag-only ghcr.io ref a workload spec names, which is what protects a
#     tag-pulled workload scaled to zero: it has no pod, so no digest reaches the set;
#   - every child manifest of a kept index, which a multi-arch pull caches beside the index;
#   - any revision whose link or manifest blob it cannot read.
# It removes every other revision link and every tag whose current link names one, and prints
# one `pruned <repo>@<digest> tags=<tags>` line per removed revision.
#
# An empty or missing KEEP_FILE prunes nothing. Rank alone would then decide, and rank alone
# deletes the images that live pods run.
#
# Usage: ghcr-mirror-prune.sh (no arguments)
# Reads MIRROR_ROOT (the store's root directory, default /var/lib/registry), KEEP_FILE (one
# sha256 digest or `<repo>:<tag>` per line, default /keep/digests), KEEP_NEWEST (default 3) and
# MIRROR_NAME (the summary line's name for the store, default ghcr-mirror).

set -u

case "${1:-}" in
  -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
esac

MIRROR_ROOT=${MIRROR_ROOT:-/var/lib/registry}
KEEP_FILE=${KEEP_FILE:-/keep/digests}
KEEP_NEWEST=${KEEP_NEWEST:-3}
V2="${MIRROR_ROOT}/docker/registry/v2"
DIGEST_RE='sha256:[0-9a-f]\{64\}'

if [ ! -r "$KEEP_FILE" ] || ! grep -q "^${DIGEST_RE}\$" "$KEEP_FILE"; then
  echo "keep set at ${KEEP_FILE} is empty or unreadable; pruned nothing"
  exit 0
fi
if [ ! -d "${V2}/repositories" ]; then
  echo "no repositories under ${V2}; pruned nothing"
  exit 0
fi

WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT

# The path of a digest's blob, which for a revision is its manifest body.
blob_path() {
  hex=${1#sha256:}
  printf '%s/blobs/sha256/%s/%s/data' "$V2" "$(printf '%s' "$hex" | cut -c1-2)" "$hex"
}

# The lines of file $2 that are, or are not, lines of file $1. awk rather than `grep -xF -f`,
# because busybox grep reads an empty pattern file as one empty pattern that matches every line.
lines_in() { awk 'FILENAME == ARGV[1] { set[$0] = 1; next } $0 in set' "$1" "$2"; }
lines_not_in() { awk 'FILENAME == ARGV[1] { set[$0] = 1; next } !($0 in set)' "$1" "$2"; }

PRUNED=0
REPOS=0
find "${V2}/repositories" -type d -name _manifests -prune | sort > "${WORK}/repos"

while read -r manifests; do
  revs="${manifests}/revisions/sha256"
  [ -d "$revs" ] || continue
  repo=${manifests#"${V2}/repositories/"}
  repo=${repo%/_manifests}
  REPOS=$((REPOS + 1))
  : > "${WORK}/all"
  : > "${WORK}/edges"
  : > "${WORK}/keep"

  for dir in "$revs"/*/; do
    [ -d "$dir" ] || continue
    digest="sha256:$(basename "$dir")"
    echo "$digest" >> "${WORK}/all"
    body=$(blob_path "$digest")
    if [ ! -r "$body" ] || [ ! -r "${dir}link" ]; then
      echo "keeping ${repo}@${digest}: revision link or manifest blob unreadable"
      echo "$digest" >> "${WORK}/keep"
      continue
    fi
    # An index or manifest list names its children in `manifests[]`. Every digest in its body
    # is taken as a child, which can only keep more than strictly needed.
    if grep -q '"manifests"' "$body"; then
      grep -o "$DIGEST_RE" "$body" | grep -vxF "$digest" | sed "s|^|${digest} |" >> "${WORK}/edges"
    fi
  done

  # A kept tag keeps the revision its current link names. A tag name holds no `/`, so one that
  # does could only climb out of the tags directory and is ignored.
  awk -v p="${repo}:" 'index($0, p) == 1 { print substr($0, length(p) + 1) }' "$KEEP_FILE" \
    | while read -r tag; do
        case "$tag" in */* | "" | . | ..) continue ;; esac
        cat "${manifests}/tags/${tag}/current/link" 2>/dev/null && echo
      done | grep "^${DIGEST_RE}\$" >> "${WORK}/keep"

  cut -d' ' -f2 "${WORK}/edges" | sort -u > "${WORK}/children"
  lines_not_in "${WORK}/children" "${WORK}/all" > "${WORK}/tops"
  lines_in "${WORK}/all" "$KEEP_FILE" >> "${WORK}/keep"

  # Newest first. A tie falls back to the digest so a run is repeatable.
  : > "${WORK}/ranked"
  while read -r digest; do
    if mtime=$(stat -c %Y "${revs}/${digest#sha256:}/link" 2>/dev/null); then
      echo "${mtime} ${digest}" >> "${WORK}/ranked"
    else
      echo "$digest" >> "${WORK}/keep"
    fi
  done < "${WORK}/tops"
  sort -k1,1nr -k2,2 "${WORK}/ranked" | head -n "$KEEP_NEWEST" | cut -d' ' -f2 >> "${WORK}/keep"

  # A kept index keeps its children, and a child that is itself an index keeps its own, so
  # repeat until the set stops growing.
  before=-1
  after=$(sort -u "${WORK}/keep" | wc -l)
  while [ "$after" -ne "$before" ]; do
    before=$after
    while read -r parent child; do
      grep -qxF "$parent" "${WORK}/keep" && echo "$child" >> "${WORK}/keep"
    done < "${WORK}/edges"
    after=$(sort -u "${WORK}/keep" | wc -l)
  done

  while read -r digest; do
    grep -qxF "$digest" "${WORK}/keep" && continue
    hex=${digest#sha256:}
    tags=""
    for tagdir in "${manifests}/tags"/*/; do
      [ -d "$tagdir" ] || continue
      if [ "$(cat "${tagdir}current/link" 2>/dev/null)" = "$digest" ]; then
        tags="${tags:+${tags},}$(basename "$tagdir")"
        rm -rf "$tagdir"
      else
        rm -rf "${tagdir}index/sha256/${hex}"
      fi
    done
    if rm -rf "${revs:?}/${hex}"; then
      PRUNED=$((PRUNED + 1))
      echo "pruned ${repo}@${digest} tags=${tags:-none}"
    else
      echo "could not remove ${repo}@${digest}"
    fi
  done < "${WORK}/all"
done < "${WORK}/repos"

echo "${MIRROR_NAME:-ghcr-mirror} prune: ${PRUNED} revision(s) pruned across ${REPOS} repositories"
