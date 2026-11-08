#!/usr/bin/env bash
# Makes the host_packages test image `homelab-host-test:<release>` for one Ubuntu release (#7664).
#
#   build-image.sh 24.04             pull the image image.pins names, or build it
#   build-image.sh 24.04 --publish   push this Dockerfile's image to GHCR unless already there
#
# A plain run pulls the pinned digest when image.pins was written for this Dockerfile, so a PR
# that changes no image input fetches nothing from snapshot.ubuntu.com. A PR that changes the
# Dockerfile, an empty pin and a failed pull each build from the Dockerfile instead.
#
# --publish tags the image `<release>-<arch>-<inputs>` and prints the image.pins line for it.
# The tag is write-once: an existing one is never rebuilt, so the digest a pin names stays tagged.
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

digest=$(pin "$release-$arch")
if [[ "$(pin inputs)" != "$inputs" ]]; then
  echo "::notice::image.pins was not written for this Dockerfile, so the image is built from it."
elif [[ -z "$digest" ]]; then
  echo "::notice::image.pins names no image for $release-$arch, so the image is built."
elif docker pull "$repo@$digest"; then
  docker tag "$repo@$digest" "$local_tag"
  echo "::notice::Pulled $repo@$digest as $local_tag."
  exit 0
else
  echo "::warning::Pulling $repo@$digest failed, so the image is built from the Dockerfile."
fi
build
