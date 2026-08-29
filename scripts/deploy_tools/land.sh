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
#   1   CI red, blocked by a change needing a hand, deploy failed, or the health gate failed
#   2   bad arguments
#   75  gave up waiting — the CI budget elapsed, or the deploy lock stayed busy
#
# Verdicts printed on stdout: settled | unhealthy | deploy-failed | nothing-to-deploy |
# blocked | needs-manual-apply. `blocked` is not a failure of this PR — something else in the
# incoming range needs an operator, and nothing was deployed. `needs-manual-apply` means this
# PR reaches a plane no deploy tag covers — the setup plane, or a shared k8s role with no
# `containers_list` entry — so it is landed but not live.
set -uo pipefail

PR=''
SINCE=''
TAGS=''
CI_TIMEOUT=900
PRIMARY=/home/ubuntu/server
BRANCH=master
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
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
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

echo "== 1/6  resolving PR #$PR"
MERGE_SHA=$(gh pr view "$PR" --json mergeCommit --jq '.mergeCommit.oid') ||
  die "could not read PR #$PR" 1
if [ -z "$MERGE_SHA" ] || [ "$MERGE_SHA" = "null" ]; then
  die "PR #$PR has no merge commit — is it merged?" 1
fi
say "merge commit $MERGE_SHA"

# Before waiting on anything. A _BROAD_MANUAL_PREFIXES change anywhere in the incoming range
# stops the tick fast-forwarding, which guarantees deploy.sh refuses as stale (exit 4) however
# green CI turns out. Landing PR #570 on 2026-08-29 spent ~6 minutes waiting for CI and then
# failed at step 4, with the blocker visible in the range before the wait began.
echo "== 2/6  pre-flight: can the tick cross what is incoming?"
git fetch -q origin "$BRANCH" || die "could not fetch origin/$BRANCH" 1
uv run python scripts/deploy_tools/deploy_tags.py blockers "origin/$BRANCH"
pf_rc=$?
case "$pf_rc" in
  0) say "nothing in the way" ;;
  3)
    echo "VERDICT: blocked (PR #$PR — an incoming change needs a hand; see above)"
    exit 1
    ;;
  *) die "pre-flight failed (exit $pf_rc) — nothing deployed" 1 ;;
esac

echo "== 3/6  waiting for master CI"
uv run python scripts/deploy_tools/await_ci.py "$MERGE_SHA" --timeout "$CI_TIMEOUT"
ci_rc=$?
case "$ci_rc" in
  0) ;;
  1) die "master CI is RED on $MERGE_SHA — nothing deployed" 1 ;;
  75) die "no CI verdict inside ${CI_TIMEOUT}s — nothing deployed" 75 ;;
  *) die "await_ci failed (exit $ci_rc) — nothing deployed" 1 ;;
esac

echo "== 4/6  GitOps tick (fetch, ff-merge, deploy what is eligible)"
./scripts/deploy_tools/gitops_tick.sh
tick_rc=$?
# 3 = lock contention, 75 = the wrapper stopped watching a run still in flight. Neither is
# a failure of the tick, and both leave the ff-merge either done or retryable next tick, so
# carry on to the scoped deploy rather than aborting the landing.
case "$tick_rc" in
  0 | 3 | 75) say "tick exit $tick_rc" ;;
  *) die "gitops tick failed (exit $tick_rc)" 1 ;;
esac

echo "== 5/6  deploying what the tick deferred"
PLANE=''
if [ -z "$TAGS" ]; then
  pr_json=$(gh pr view "$PR" --json files,changedFiles) || die "could not read PR files" 1
  # What a deploy tag cannot reach. deploy.yml is a containers_list loop, so a setup-plane
  # change needs initial_setup.yml and derives no tag at all — which land.sh used to report
  # as nothing-to-deploy. A shared k8s role (manifests, seed-volume, …) has no entry in that
  # list either, so it needs a full deploy. Computed whether or not tags were derived: a PR
  # can touch a deployable role AND one of those planes, and then the deploy succeeds while
  # half the change is unapplied, under a `settled` verdict.
  PLANE=$(uv run python scripts/deploy_tools/land_tags.py --plane --json "$pr_json") ||
    die "plane classification failed" 1
  derived=$(uv run python scripts/deploy_tools/land_tags.py --json "$pr_json") ||
    die "tag derivation failed" 1
  source_kind=${derived%% *}
  TAGS=${derived#* }
  if [ "$source_kind" = "fallback" ]; then
    [ -n "$SINCE" ] ||
      die "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>"
    say "file list truncated; deriving from the diff since $SINCE instead"
    # Resolve to a tag list HERE rather than handing deploy.sh --changed. Both run the same
    # deriver, but only this leaves step 5 something to health-check: --changed resolves
    # internally, so deploy.sh would deploy real services while the verdict call received an
    # empty --tags and reported settled having checked nothing — on the large-PR path, where
    # verification matters most.
    TAGS=$(uv run python scripts/deploy_tools/deploy_tags.py changed "$SINCE")
    changed_rc=$?
    case "$changed_rc" in
      0) ;;
      3) die "the change is broad and maps to no service list — deploy it by hand" 1 ;;
      *) die "deploy_tags.py changed failed (exit $changed_rc)" 1 ;;
    esac
  fi
fi

if [ -z "$TAGS" ]; then
  if [ -n "$PLANE" ]; then
    echo "  it needs applying by hand: $PLANE"
    echo "VERDICT: needs-manual-apply (PR #$PR reaches no service tag, but is not done)"
    exit 1
  fi
  echo "VERDICT: nothing-to-deploy (PR #$PR touched no service)"
  exit 0
fi

deploy_rc=0
attempt=1
while [ "$attempt" -le "$LOCK_RETRIES" ]; do
  ./scripts/deploy.sh --tags "$TAGS"
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

# 4 = the tree is behind origin/master. CLAUDE.md classes this a resume point: pull again,
# never --skip-staleness-check. It happens when someone merges during the CI wait, so the tick
# at step 4 had a newer tip than the pre-flight checked. One more tick fetches and crosses it.
# Bounded at a single retry on purpose — if the new tip carries a broad-manual change the tick
# will never cross it, and re-ticking forever would hide that behind a stalled landing.
if [ "$deploy_rc" -eq 4 ]; then
  say "tree went stale mid-landing (someone merged during the wait); re-ticking once"
  git fetch -q origin "$BRANCH" || die "could not fetch origin/$BRANCH" 1
  uv run python scripts/deploy_tools/deploy_tags.py blockers "origin/$BRANCH"
  retry_pf_rc=$?
  if [ "$retry_pf_rc" -eq 3 ]; then
    echo "VERDICT: blocked (PR #$PR — a change needing a hand landed during the wait; see above)"
    exit 1
  fi
  ./scripts/deploy_tools/gitops_tick.sh
  ./scripts/deploy.sh --tags "$TAGS"
  deploy_rc=$?
fi

case "$deploy_rc" in
  0) ;;
  2)
    # A tag matched no service, so deploy.sh refused the WHOLE list and nothing was deployed
    # — including every valid service beside the bad tag. This read as `nothing-to-deploy`
    # and exit 0 until 2026-08-29, which is how PR #617 left 22 digest pins undeployed behind
    # a green verdict. The derivation is fixed upstream; this stays as the backstop, because
    # a tag list deploy.sh will not accept is a defect and never a finished landing.
    echo "VERDICT: deploy-failed (PR #$PR — a derived tag matched no service, so nothing deployed; tags: $TAGS)"
    exit 1
    ;;
  75) die "deploy lock stayed busy after $LOCK_RETRIES attempts — nothing deployed" 75 ;;
  *)
    echo "VERDICT: deploy-failed (PR #$PR, exit $deploy_rc)"
    exit 1
    ;;
esac

echo "== 6/6  health verdict"
# --status 0 because the deploy above already succeeded; this call is here for the health
# gate, which is the half `ansible-playbook` exiting 0 cannot speak to. --no-post keeps the
# verdict in this session rather than duplicating it onto Discord, where the --detach path
# already reports.
uv run python scripts/deploy_tools/deploy_detach_notify.py \
  --status 0 --log /dev/null --tags "$TAGS" --no-post
verdict_rc=$?

if [ -n "$PLANE" ]; then
  echo "  STILL UNAPPLIED, and no deploy tag covers it: $PLANE"
fi

if [ "$verdict_rc" -eq 0 ]; then
  if [ -n "$PLANE" ]; then
    echo "VERDICT: needs-manual-apply (PR #$PR, $MERGE_SHA — services deployed, the plane above not)"
    exit 1
  fi
  echo "VERDICT: settled (PR #$PR, $MERGE_SHA, tags: $TAGS)"
  exit 0
fi
echo "VERDICT: unhealthy (PR #$PR, $MERGE_SHA, tags: $TAGS)"
exit 1
