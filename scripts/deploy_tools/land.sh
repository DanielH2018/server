#!/usr/bin/env bash
#
# land.sh — follow a merged PR through to a verified deploy, in one invocation.
#
# WHY ONE SCRIPT RATHER THAN A CHAIN. A session cannot write this sequence inline: shell
# control flow and command substitution defeat the worktree containment check, which
# refuses with "too complex to verify that it stays inside the worktree". A single script
# invocation is accepted, loops and all (verified 2026-08-29). Run it backgrounded and the
# session is re-invoked when it exits, instead of hand-polling CI for five to fifteen
# minutes — 835 polls across 213 wait episodes before this existed.
#
# WHAT THIS SCRIPT DOES NOT DO. It holds no check of its own: no health logic, no tag
# validation, no staleness logic. deploy.sh owns the lock and the refusals, gitops_tick.sh
# owns the tick, deploy_detach_notify.py owns the health verdict, await_ci.py owns the CI
# wait. A check appearing in here is a bug, not a feature — it would be a second
# implementation that drifts from the first.
#
# Usage:
#   land.sh --pr 574 --since <pre-merge-sha>
#   land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand
#
# Exit codes:
#   0   deployed and settled, or there was nothing to deploy
#   1   CI red, deploy failed, or the health gate failed
#   2   bad arguments
#   75  gave up waiting — the CI budget elapsed, or the deploy lock stayed busy
set -uo pipefail

PR=''
SINCE=''
TAGS=''
CI_TIMEOUT=900
PRIMARY=/home/ubuntu/server
LOCK_RETRIES=5
LOCK_BACKOFF=60

die() {
  printf 'land: %s\n' "$1" >&2
  exit "${2:-2}"
}
say() { printf '  %s\n' "$1"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --pr)
      PR="${2:?--pr needs a number}"
      shift 2
      ;;
    --since)
      SINCE="${2:?--since needs a SHA}"
      shift 2
      ;;
    --tags)
      TAGS="${2:?--tags needs a list}"
      shift 2
      ;;
    --ci-timeout)
      CI_TIMEOUT="${2:?--ci-timeout needs seconds}"
      shift 2
      ;;
    -h | --help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument '$1'" ;;
  esac
done

[ -n "$PR" ] || die "--pr is required"

# deploy.sh resolves its checkout with `git rev-parse --show-toplevel`, so it renders from
# the WORKING DIRECTORY, not from the path it was invoked by. After a squash merge a
# worktree is behind master, so deploying from one is refused (exit 4) — and would render
# stale templates if it were not. Everything below therefore runs from the primary checkout.
cd "$PRIMARY" || die "cannot cd to $PRIMARY" 1

echo "== 1/5  resolving PR #$PR"
MERGE_SHA=$(gh pr view "$PR" --json mergeCommit --jq '.mergeCommit.oid') ||
  die "could not read PR #$PR" 1
if [ -z "$MERGE_SHA" ] || [ "$MERGE_SHA" = "null" ]; then
  die "PR #$PR has no merge commit — is it merged?" 1
fi
say "merge commit $MERGE_SHA"

echo "== 2/5  waiting for master CI"
uv run python scripts/deploy_tools/await_ci.py "$MERGE_SHA" --timeout "$CI_TIMEOUT"
ci_rc=$?
case "$ci_rc" in
  0) ;;
  1) die "master CI is RED on $MERGE_SHA — nothing deployed" 1 ;;
  75) die "no CI verdict inside ${CI_TIMEOUT}s — nothing deployed" 75 ;;
  *) die "await_ci failed (exit $ci_rc) — nothing deployed" 1 ;;
esac

echo "== 3/5  GitOps tick (fetch, ff-merge, deploy what is eligible)"
./scripts/deploy_tools/gitops_tick.sh
tick_rc=$?
# 3 = lock contention, 75 = the wrapper stopped watching a run still in flight. Neither is
# a failure of the tick, and both leave the ff-merge either done or retryable next tick, so
# carry on to the scoped deploy rather than aborting the landing.
case "$tick_rc" in
  0 | 3 | 75) say "tick exit $tick_rc" ;;
  *) die "gitops tick failed (exit $tick_rc)" 1 ;;
esac

echo "== 4/5  deploying what the tick deferred"
if [ -z "$TAGS" ]; then
  pr_json=$(gh pr view "$PR" --json files,changedFiles) || die "could not read PR files" 1
  derived=$(uv run python scripts/deploy_tools/land_tags.py --json "$pr_json") ||
    die "tag derivation failed" 1
  source_kind=${derived%% *}
  TAGS=${derived#* }
  if [ "$source_kind" = "fallback" ]; then
    [ -n "$SINCE" ] ||
      die "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>"
    say "file list truncated; widening to --changed $SINCE"
    TAGS=''
  fi
fi

if [ -z "$TAGS" ] && [ -z "$SINCE" ]; then
  echo "VERDICT: nothing-to-deploy (PR #$PR touched no service)"
  exit 0
fi

deploy_rc=0
attempt=1
while [ "$attempt" -le "$LOCK_RETRIES" ]; do
  if [ -n "$TAGS" ]; then
    ./scripts/deploy.sh --tags "$TAGS"
  else
    ./scripts/deploy.sh --changed "$SINCE"
  fi
  deploy_rc=$?
  # 75 = the git-tree lock stayed busy (the 30-min timer, or another session). Nothing was
  # deployed, so this is a resume point rather than a failure.
  if [ "$deploy_rc" -ne 75 ]; then
    break
  fi
  say "deploy lock busy (attempt $attempt/$LOCK_RETRIES); retrying in ${LOCK_BACKOFF}s"
  sleep "$LOCK_BACKOFF"
  attempt=$((attempt + 1))
done

case "$deploy_rc" in
  0) ;;
  2)
    echo "VERDICT: nothing-to-deploy (no service tag matched)"
    exit 0
    ;;
  75) die "deploy lock stayed busy after $LOCK_RETRIES attempts — nothing deployed" 75 ;;
  *)
    echo "VERDICT: deploy-failed (PR #$PR, exit $deploy_rc)"
    exit 1
    ;;
esac

echo "== 5/5  health verdict"
# --status 0 because the deploy above already succeeded; this call is here for the health
# gate, which is the half `ansible-playbook` exiting 0 cannot speak to. --no-post keeps the
# verdict in this session rather than duplicating it onto Discord, where the --detach path
# already reports.
uv run python scripts/deploy_tools/deploy_detach_notify.py \
  --status 0 --log /dev/null --tags "$TAGS" --no-post
verdict_rc=$?

if [ "$verdict_rc" -eq 0 ]; then
  echo "VERDICT: settled (PR #$PR, $MERGE_SHA, tags: ${TAGS:-<changed>})"
  exit 0
fi
echo "VERDICT: unhealthy (PR #$PR, $MERGE_SHA, tags: ${TAGS:-<changed>})"
exit 1
