#!/usr/bin/env bash
# The daniel-server half of the staging expectation check. Piped over ssh by
# staging_expectations.py, never invoked directly.
#
# Takes `<hostname> <path>` pairs as ARGUMENTS and prints `<hostname> <path> <status>` for each.
# Arguments rather than stdin because `bash -s` consumes stdin as the script itself, so a probe
# list piped alongside it would silently never be read.
#
# It makes NO judgement: the caller owns the comparison, so the expected values live in one
# place (the inventory) rather than being split across a shell script it cannot test.
#
# The domain is resolved here because secrets-staging.yml is encrypted to daniel-server's age
# key alone — daniel-box cannot decrypt it. It is never printed.
set -uo pipefail

REPO=/home/ubuntu/server
VIP=192.168.140.240

cd "$REPO" || { echo "staging-expect: no checkout at $REPO" >&2; exit 70; }

DOMAIN=$(sops -d --extract '["domain"]' ansible/vars/secrets-staging.yml 2>/dev/null) || {
  echo "staging-expect: could not read the staging domain" >&2
  exit 70
}
[ -n "$DOMAIN" ] || { echo "staging-expect: staging domain is empty" >&2; exit 70; }

while [ "$#" -ge 2 ]; do
  host="$1"
  path="$2"
  shift 2
  fqdn="${host}.local.${DOMAIN}"
  # -k because staging serves the Traefik default cert by design (Decision 4 of the Phase A/B
  # spec): it touches no real ACME, so a cert error here is the expected state, not a finding.
  # --resolve pins the VIP so this measures STAGING and never whatever DNS points the shared
  # name at, which is prod.
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
    --resolve "${fqdn}:443:${VIP}" "https://${fqdn}${path}") || code=000
  echo "$host $path $code"
done
