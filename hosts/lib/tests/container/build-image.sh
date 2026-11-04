#!/usr/bin/env bash
# Builds the host_packages test image for one Ubuntu release through BuildKit's GitHub Actions
# cache, so a run with an unchanged Dockerfile fetches nothing from snapshot.ubuntu.com (#7133).
#
#   build-image.sh 24.04
#
# The caller sets up the prerequisites: docker/setup-buildx-action, because the default `docker`
# driver cannot export to `type=gha`, and crazy-max/ghaction-github-runtime, because a `run:` step
# otherwise has no token for the cache service.
#
# Each release and architecture has one scope, since the fetched layers differ across both. A
# pull_request run only reads: GitHub would file its write under the PR's ref, where no other
# branch can read it, and the repository's cache quota would pay for it. The
# host-test-image-cache workflow seeds the scopes on master.
set -euo pipefail

release=$1
scope="host-test-image-$release-$(dpkg --print-architecture)"

build=(
  docker buildx build --load --progress=plain
  --build-arg "RELEASE=$release"
  --secret "id=runner-ca,src=/etc/ssl/certs/ca-certificates.crt"
  -t "homelab-host-test:$release"
  "$(dirname "$0")"
)
cache=(--cache-from "type=gha,scope=$scope")
if [[ "${GITHUB_EVENT_NAME:-}" != pull_request ]]; then
  # ignore-error: a cache service that refuses the write costs the next run time, never this one.
  cache+=(--cache-to "type=gha,scope=$scope,mode=min,ignore-error=true")
fi

# BuildKit reports an empty scope as a miss, and builds from the mirror. A cache service that
# fails the import fails the solve instead, so the build runs once more without the cache.
if ! "${build[@]}" "${cache[@]}"; then
  echo "::warning::The cached build of ubuntu:$release failed, so it runs again without the cache."
  "${build[@]}"
fi
