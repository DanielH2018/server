#!/usr/bin/env bash
# Makes the host_packages test image `homelab-host-test:<release>` for one Ubuntu release (#7664).
#
#   build-image.sh 24.04             pull the image image.pins names, or build it
#   build-image.sh 24.04 --publish   push this Dockerfile's image to GHCR unless already there
#
# A plain run pulls the pinned digest when image.pins was written for this Dockerfile, so a PR
# that changes no image input fetches nothing from snapshot.ubuntu.com. A stale pin, or a site
# pinned `-`, pulls this Dockerfile's tag instead, which master publishes after the merge. Any
# token with packages: write can repoint a tag, so a tag pull keeps the image only when its
# org.homelab.dockerfile-sha256 label is this Dockerfile's hash. A PR that changes the Dockerfile
# has no such tag yet, so it builds from the Dockerfile, as does a pull that fails or is refused.
#
# --publish tags the image `<release>-<arch>-<inputs>` and prints the image.pins line for it.
# The publisher never rebuilds an existing tag, so the digest a pin names stays tagged.
#
# GHCR_TOKEN and GHCR_USER, when set, log in to GHCR first. A private package needs them.
set -euo pipefail

release=$1
mode=${2:-}
here=$(dirname "$0")
arch=$(dpkg --print-architecture)
repo=ghcr.io/danielh2018/homelab-host-test
local_tag="homelab-host-test:$release"
# DECIDED: only the Dockerfile is hashed. The script passes RELEASE alone as a build argument and
# the tag carries it, so an edit to the pull logic here still exercises the pull.
inputs=$(sha256sum "$here/Dockerfile" | cut -c1-64)
remote_tag="$repo:$release-$arch-${inputs:0:12}"

pin() { awk -v key="$1" '$1 == key { print $2 }' "$here/image.pins"; }

build() {
  docker buildx build --load --progress=plain \
    --build-arg "RELEASE=$release" \
    --label "org.homelab.dockerfile-sha256=$inputs" \
    --secret "id=runner-ca,src=/etc/ssl/certs/ca-certificates.crt" \
    -t "$local_tag" \
    "$here"
}

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:?}" --password-stdin \
    || echo "::warning::docker login ghcr.io failed; a private image will not pull"
fi

if [[ "$mode" == --publish ]]; then
  if ! answer=$(docker buildx imagetools inspect "$remote_tag" 2>&1); then
    # Only a "not found" means absent; a registry or auth error would overwrite a pinned tag.
    if [[ "$answer" != *"not found"* ]]; then
      echo "::error::Whether $remote_tag exists is unknown, so nothing is pushed: $answer" >&2
      exit 1
    fi
    build
    docker tag "$local_tag" "$remote_tag"
    docker push "$remote_tag"
  fi
  digest=$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$remote_tag")
  echo "$release-$arch $digest"
  exit 0
fi

# Pulls $1 as $local_tag, naming the digest it got. Refuses an image built from another Dockerfile.
pull() {
  docker pull "$1" || return 1
  local label
  label=$(docker inspect --format '{{ index .Config.Labels "org.homelab.dockerfile-sha256" }}' "$1")
  if [[ "$label" != "$inputs" ]]; then
    echo "::warning::$1 was built from Dockerfile ${label:-<unlabelled>}, not $inputs, so it is not used."
    return 1
  fi
  docker tag "$1" "$local_tag"
  echo "::notice::Pulled $1 as $local_tag ($(docker inspect --format '{{index .RepoDigests 0}}' "$1"))."
}

digest=$(pin "$release-$arch")
if [[ "$(pin inputs)" == "$inputs" && "$digest" == sha256:* ]]; then
  pull "$repo@$digest" && exit 0
  echo "::warning::Pulling the pinned $repo@$digest failed."
fi
if pull "$remote_tag"; then
  if [[ "$(pin inputs)" != "$inputs" || "$digest" != sha256:* ]]; then
    echo "::warning::image.pins pins no digest of this Dockerfile for $release-$arch. Copy the lines from the host-test-image run's summary into it."
  fi
  exit 0
fi
echo "::notice::No image of this Dockerfile pulled, so it is built from the Dockerfile."
build
