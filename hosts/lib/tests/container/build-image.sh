#!/usr/bin/env bash
# Builds the host_packages test image for one Ubuntu release through BuildKit's GitHub Actions
# cache, so a run with an unchanged Dockerfile fetches nothing from snapshot.ubuntu.com (#7133).
#
#   build-image.sh 24.04 [--seed]
#
# The caller sets up the prerequisites: docker/setup-buildx-action, because the default `docker`
# driver cannot export to `type=gha`, and crazy-max/ghaction-github-runtime, because a `run:` step
# otherwise has no token for the cache service.
#
# Each release and architecture has one scope, since the fetched layers differ across both.
# BuildKit reads an empty scope or a failed import as a miss, and builds from the mirror.
#
# --seed also writes the scope, and only the host-test-image-cache workflow passes it. There is
# no ignore-error, so a write that fails turns that workflow red instead of leaving PRs on the
# mirror with nothing to show why.
set -euo pipefail

release=$1
scope="host-test-image-$release-$(dpkg --print-architecture)"

cache=(--cache-from "type=gha,scope=$scope")
if [[ "${2:-}" == --seed ]]; then
  cache+=(--cache-to "type=gha,scope=$scope,mode=min")
fi

docker buildx build --load --progress=plain "${cache[@]}" \
  --build-arg "RELEASE=$release" \
  --secret "id=runner-ca,src=/etc/ssl/certs/ca-certificates.crt" \
  -t "homelab-host-test:$release" \
  "$(dirname "$0")"
