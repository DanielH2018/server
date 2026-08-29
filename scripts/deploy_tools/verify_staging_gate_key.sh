#!/usr/bin/env bash
# Prove the staging gate's restricted ssh key is confined to its dispatcher. Run on daniel-box.
#
#   ./scripts/deploy_tools/verify_staging_gate_key.sh [<sha>]
#
# WHY THIS IS A SCRIPT AND NOT TWO COMMANDS IN A RUNBOOK. The obvious two-command version is
# unsound, and it failed in exactly its unsound direction on 2026-08-29. The negative check was
#
#     ssh -i <key> -o IdentitiesOnly=yes daniel-server "bash -s" </dev/null; echo $?
#
# which printed 0 — the signal of the restriction FAILING — when the real cause was that the key
# would not load at all, so ssh silently fell back to a default identity and ran a normal shell.
# `IdentitiesOnly=yes` does not prevent that: the DEFAULT identity files still count as
# configured, so it bounds which keys are offered without guaranteeing that ours is one of them.
#
# A check that reports "your security control is broken" when the truth is "your key file is
# unreadable" is worse than no check, because the next person acts on the wrong diagnosis. So:
#
#   1. The key must load AND match the committed public half BEFORE any connection is made. A
#      load failure is its own exit code and never reaches ssh.
#   2. The negative case demands BOTH a 71 and the dispatcher's own refusal marker on stderr. A
#      fallback to another key cannot produce that marker, so "fell back" and "restriction
#      bypassed" stay distinguishable — which a bare exit code cannot do.
set -uo pipefail

OK=0
KEY_UNUSABLE=10    # the key does not load, or is not the key the role authorized
FELL_BACK=11       # something other than this key authenticated
RESTRICTION_OPEN=12 # this key authenticated and was NOT confined to the dispatcher
NO_VERDICT=13      # the gate could not be asked; says nothing about the restriction

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEY=${STAGING_GATE_KEY:-/etc/gitops-deploy/staging_gate_ed25519}
PUB="$REPO_ROOT/ansible/roles/setup/hypervisor/files/staging-gate.pub"
HOST=${STAGING_GATE_HOST:-daniel-server}

# The dispatcher's refusal marker. It prints this and nothing else on a refused request, so its
# presence proves OUR key reached OUR forced command.
MARKER="staging-gate: refused"

# Kept as a function over (rc, stderr) so ansible/tests/test_verify_staging_gate_key.py can drive
# the same verdict logic without a network, and so each failure mode has a distinct name.
classify_negative() {
  local rc="$1" err="$2"

  if [ "$rc" -eq 0 ]; then
    echo "RESTRICTION_OPEN: \`bash -s\` succeeded — this key is not confined to the dispatcher" >&2
    return "$RESTRICTION_OPEN"
  fi
  if [ "$rc" -eq 255 ]; then
    echo "NO_VERDICT: ssh itself failed (transport, host key, or auth refused outright)" >&2
    return "$NO_VERDICT"
  fi
  case "$err" in
    *"$MARKER"*) ;;
    *)
      echo "FELL_BACK: exit $rc but no dispatcher refusal on stderr — something other than the" >&2
      echo "           restricted key answered, so this run proves nothing about the restriction" >&2
      return "$FELL_BACK"
      ;;
  esac
  if [ "$rc" -ne 71 ]; then
    echo "FELL_BACK: dispatcher answered but exit was $rc, not the expected 71" >&2
    return "$FELL_BACK"
  fi
  return "$OK"
}

# PRECONDITION. Runs before any connection, so an unusable key can never be mistaken for a
# broken restriction. This is the check whose absence caused the 2026-08-29 misdiagnosis.
assert_key_is_the_authorized_one() {
  local derived committed
  if ! derived=$(ssh-keygen -y -f "$KEY" 2>&1); then
    echo "KEY_UNUSABLE: $KEY does not load: $derived" >&2
    echo "              (a key one byte short of its trailing newline fails exactly this way)" >&2
    return "$KEY_UNUSABLE"
  fi
  committed=$(cut -d' ' -f1,2 <"$PUB")
  if [ "$(echo "$derived" | cut -d' ' -f1,2)" != "$committed" ]; then
    echo "KEY_UNUSABLE: $KEY loads but is not the key $PUB authorizes" >&2
    return "$KEY_UNUSABLE"
  fi
  echo "ok: $KEY loads and matches the committed public half"
  return "$OK"
}

main() {
  local sha="${1:-}" err rc
  assert_key_is_the_authorized_one || exit $?

  # IdentityAgent=none removes agent keys; IdentitiesOnly bounds the configured set. Neither
  # fully excludes the default identity files, which is why the marker check above is what
  # actually decides rather than these options.
  local -a SSH=(ssh -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none
                -o PreferredAuthentications=publickey -i "$KEY")

  echo "--- negative: a bare shell must be REFUSED by the dispatcher ---"
  err=$("${SSH[@]}" "$HOST" "bash -s" </dev/null 2>&1 >/dev/null)
  rc=$?
  classify_negative "$rc" "$err" || exit $?
  echo "ok: refused with 71 and the dispatcher's own marker — the key cannot open a shell"

  if [ -z "$sha" ]; then
    echo "no sha given; skipping the positive check (pass one to run a real gate)"
    return "$OK"
  fi

  echo "--- positive: a well-formed request must reach the gate ---"
  "${SSH[@]}" "$HOST" "gate $sha freshrss"
  rc=$?
  if [ "$rc" -eq 71 ]; then
    echo "the dispatcher refused a request that should have been valid" >&2
    exit "$RESTRICTION_OPEN"
  fi
  echo "gate returned $rc (0 PASS / 1 REJECTED / 70 prep — any of these proves it was reached)"
  return "$OK"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
