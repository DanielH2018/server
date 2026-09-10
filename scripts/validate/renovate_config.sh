#!/usr/bin/env bash
# Validate renovate.json against a freshly-resolved Renovate, riding out npm's publish window.
#
# WHY THE INSTALL IS RETRIED AND THE VALIDATION IS NOT. Renovate publishes to npm roughly
# eight times a day. For a few minutes after each publish the registry's `latest` tag already
# names the new version while its tarball has not reached every CDN edge, so the fetch fails:
#
#   npm error 404 Not Found - GET https://registry.npmjs.org/renovate/-/renovate-44.75.0.tgz
#
# Three master CI runs failed exactly that way on 2026-09-09/10, each 1.5-2 minutes after a
# publish, and the next run six minutes later passed on the same version (issue #1269). That
# is a transport failure, so the install loops with a backoff. A validation failure is the
# verdict this script exists to deliver, so it runs once and its exit code is final.
#
# WHY `renovate@latest` AND NOT A PIN. The validator must match the hosted Mend app that
# actually consumes the config, which tracks latest; the reasoning is on the `renovate-config`
# job in .github/workflows/ci.yml. Pinning here would silently defeat that canary.
#
# Both the PR-scoped job in ci.yml and the daily canary in renovate-config-canary.yml run
# this script, so the retry protects both and the semantics cannot drift between them.
set -euo pipefail

attempts="${RENOVATE_INSTALL_ATTEMPTS:-6}"
backoff="${RENOVATE_INSTALL_BACKOFF:-60}"
prefix="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/renovate-config-validator"
config="${1:-renovate.json}"

attempt=1
until npm install --prefix "$prefix" --no-save --no-package-lock --no-audit --no-fund renovate@latest; do
  if [ "$attempt" -ge "$attempts" ]; then
    echo "::error::renovate@latest did not install after $attempt attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  echo "npm install failed; retrying in ${backoff}s (attempt $attempt of $attempts)." \
    "A tarball 404 minutes after a Renovate publish is npm's replication window, not a config problem."
  sleep "$backoff"
done

exec "$prefix/node_modules/.bin/renovate-config-validator" --strict "$config"
