#!/usr/bin/env bash
# Install a freshly-resolved Renovate, riding out npm's publish window, and run the checks named.
#
# Usage: renovate_config.sh [validate] [extract]   (both when none is named)
#
#   validate  renovate-config-validator --strict over renovate.json.
#   extract   `renovate --platform=local --dry-run=extract`, whose report
#             scripts/validate/renovate_extract.py checks against renovate_expected_deps.tsv.
#             renovate.json is read as the repository's config, the way the hosted app reads
#             it. Passing it as RENOVATE_CONFIG_FILE as well would merge it twice and report
#             every custom manager's deps twice.
#
# Run it from the repository root.
#
# WHY THE INSTALL IS RETRIED AND THE CHECKS ARE NOT. Renovate publishes to npm roughly
# eight times a day. For a few minutes after each publish the registry's `latest` tag already
# names the new version while its tarball has not reached every CDN edge, so the fetch fails:
#
#   npm error 404 Not Found - GET https://registry.npmjs.org/renovate/-/renovate-44.75.0.tgz
#
# Such a failure appears 1.5-2 minutes after a publish, and a run six minutes later passes on
# the same version. That is a transport failure, so the install loops with a backoff. A
# check failure is the verdict this script exists to deliver, so each check runs once and its
# exit code is final.
#
# WHY `renovate@latest` AND NOT A PIN. The validator must match the hosted Mend app that
# actually consumes the config, which tracks latest; the reasoning is on the `renovate-config`
# job in .github/workflows/ci.yml. Pinning here would silently defeat that canary.
#
# Both the PR-scoped job in ci.yml and the daily canary in renovate-config-canary.yml run
# this script, so the retry protects both and the semantics cannot drift between them.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

attempts="${RENOVATE_INSTALL_ATTEMPTS:-6}"
backoff="${RENOVATE_INSTALL_BACKOFF:-60}"
tmp="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
prefix="$tmp/renovate-config-validator"
checks=("$@")
if [ "${#checks[@]}" -eq 0 ]; then
  checks=(validate extract)
fi
for check in "${checks[@]}"; do
  case "$check" in
    validate | extract) ;;
    *)
      echo "renovate_config.sh: unknown check '$check'; expected validate or extract" >&2
      exit 64
      ;;
  esac
done

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

status=0
for check in "${checks[@]}"; do
  if [ "$check" = validate ]; then
    "$prefix/node_modules/.bin/renovate-config-validator" --strict renovate.json || status=$?
  else
    report="$tmp/renovate-extract.json"
    if LOG_LEVEL=warn "$prefix/node_modules/.bin/renovate" --platform=local --dry-run=extract \
      --report-type=file --report-path="$report"; then
      uv run python scripts/validate/renovate_extract.py check "$report" || status=$?
    else
      status=$?
    fi
  fi
done
exit "$status"
