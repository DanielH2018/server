#!/usr/bin/env bash
#
# gh-quota: GitHub API spend per session on this host, beside GitHub's own count.
#
#   gh-quota status          a count line per identity and bucket, the sessions over the
#                            share, and each identity's writes against the secondary limits
#   gh-quota status --all    every session, with its calls by class
#   gh-quota status --json   the same windows as one line of compact JSON
#   gh-quota mode            prints `record` or `off`
#
# Runs /usr/lib/homelab/gh_quota_cli.py, so Python imports gh_quota.py from beside it.
# docs/github-quota.md has the design.
set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done
exec /usr/bin/python3 /usr/lib/homelab/gh_quota_cli.py "$@"
