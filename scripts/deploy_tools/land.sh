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
# ALWAYS REDIRECT STDOUT AND STDERR TO A FILE. A backgrounded Bash call hands this script a
# non-blocking pipe, and Ansible refuses to start on one ("Ansible requires blocking IO on
# stdin/stdout/stderr. Non-blocking file handles detected: <stdout>, <stderr>"), so the deploy
# phase fails with nothing deployed and a VERDICT: deploy-failed that names Ansible rather
# than the harness. `> "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1` is the whole fix.
#
# WHAT THIS SCRIPT DOES NOT DO. It holds no check of its own: no health logic, no tag
# validation, no staleness logic. deploy.sh owns the lock and the refusals, gitops_tick.sh
# owns the tick, deploy_detach_notify.py owns the health verdict, await_ci.py owns the CI
# wait. A check appearing in here is a bug, not a feature — it would be a second
# implementation that drifts from the first.
#
# Usage:
#   land.sh --pr 574 --since <pre-merge-sha>
#   land.sh --pr 574 --since <sha> --await-merge   # arm `gh pr merge --auto` first, then this
#   land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand
#
# --await-merge polls the PR's state until it is merged, so the session's whole procedure is
# `gh pr create`, `gh pr merge --squash --auto`, and ONE backgrounded land.sh. Every landing
# on 2026-09-01 hand-wrote that wait as an `until MERGED; sleep 30` loop. It asks await_ci.py
# for the PR's own CI verdict alongside that state, which is the opposite of the hand-polling
# this script replaced: one bounded read that ENDS the wait on a red, rather than a session
# watching a green arrive.
#
# Exit codes:
#   0   deployed and settled, or there was nothing to deploy
#   1   CI red, blocked by a change needing a hand, deploy failed, the health gate failed, or
#       the PR was closed unmerged, conflicts with master, or its own CI is red
#   2   bad arguments
#   75  gave up waiting — the merge budget or CI budget elapsed, the deploy lock stayed busy,
#       the tick was skipped for lock contention every time, or the tick has not yet crossed
#       origin
#
# Verdicts printed on stdout: settled | unhealthy | deploy-failed | nothing-to-deploy |
# blocked | needs-manual-apply | deferred | merge-conflict | pr-ci-red | merge-timeout |
# ci-red | ci-timeout | lock-busy.
# The last four are the `die` exits that used to reach the Landings board as one `aborted`
# verdict: the 45 landings before 2026-09-02 held four of them, two at exactly the 2700s
# merge budget and two at the CI budget, and nothing on the board told them apart from a
# lock that stayed busy. `blocked` is not a failure of this PR — something
# else in the incoming range needs an operator, and nothing was deployed. `needs-manual-apply`
# means this PR reaches something neither a deploy tag nor the tick covers — a bring-up
# playbook, a setup role initial_setup.yml does not include, a shared k8s role with no
# `containers_list` entry, or a rotated secret, whose value lives in no role's template at all
# — so it is landed but not live. `deferred` means the tick applies this PR itself (a setup
# role or the deploy plane) and has not crossed origin yet, usually because a newer merge's
# CI is still running; the next tick does it, and nothing is wrong with this PR.
# `merge-conflict` and `pr-ci-red` are the merge wait ending early, on the two states an
# armed auto-merge never recovers from: the PR needs a rebase, or its own CI is red. Each
# names its own remedy, where `merge-timeout` could only say "nobody merged it yet".
# `pr-ci-red` is the PR's CI before the merge; `ci-red` is master's after it.
set -uo pipefail

PR=''
SINCE=''
TAGS=''
CI_TIMEOUT=900
# --await-merge: poll `gh pr view` until the PR is merged before doing anything else, so
# `gh pr create` → `gh pr merge --auto` → one backgrounded land.sh is the whole procedure.
# Sized for a PR run plus queueing behind other PRs' runs; a PR that is still open after this
# is not being merged, and the session should look at why.
AWAIT_MERGE=0
MERGE_TIMEOUT=2700
# Both overridable so the merge-wait loop can be exercised by a test with a stubbed `gh`, in a
# temporary checkout and without a 30s sleep per poll. Nothing operational sets them.
MERGE_POLL=${LAND_MERGE_POLL:-30}
PRIMARY=${LAND_PRIMARY:-/home/ubuntu/server}
BRANCH=master
LOCK_RETRIES=5
LOCK_BACKOFF=60

#######################################
# Stop the landing with a message on stderr.
# Globals:
#   LAND_VERDICT (set when a verdict is given, so the EXIT trap logs it)
#   PR
# Arguments:
#   the message, the exit code (default 2), an optional verdict name
# Outputs:
#   `land: <message>` on stderr; `VERDICT: <verdict> (...)` on stdout when one is given
#######################################
die() {
  printf 'land: %s\n' "$1" >&2
  if [ -n "${3:-}" ]; then
    LAND_VERDICT=$3
    echo "VERDICT: $3 (PR #${PR:-?} — $1)"
  fi
  exit "${2:-2}"
}
say() { printf '  %s\n' "$1"; }

# One logfmt line per landing into syslog, which promtail already ships to Loki — the same
# path deploy.sh's `deploy-annotation` takes, read by the Landings dashboard. This is the
# only record of where a landing's time goes: how long the merge took to arrive, how long
# master CI took after it, the tick, the deploy. Every earlier number here was one PR timed
# by hand. Emitted from an EXIT trap so every verdict and every `die` is counted, including
# the ones that end in exit 75 with nothing deployed. Fire-and-forget by construction: a
# landing that succeeded must never report failure because logging it did not.
T_START=$SECONDS
T_MERGED=''
T_CI=''
T_TICK=''
T_DEPLOY=''
LAND_VERDICT=''
MERGE_SHA=''
TAGS_LABEL=''
# Seconds spent in tick or deploy attempts that ended in lock contention, backoff included,
# and what held the lock the first time. Part of `tick` and `deploy` on the board, not a
# fifth phase: the contended attempt is inside those stamps. deploy.sh alone can sit 3000s
# on the lock before its exit 75, all of it booked as deploy time until this field existed.
LOCK=/var/lock/server-git-tree.lock
LOCK_WAITED=0
LOCK_HOLDER=''
#######################################
# Record one attempt that lost the tree lock, and name the holder on the first one.
# Globals:
#   LOCK, LOCK_WAITED, LOCK_HOLDER
# Arguments:
#   seconds the attempt spent before giving up
# Outputs:
#   the holder's age and command on stdout, once
#######################################
note_lock_contention() {
  LOCK_WAITED=$((LOCK_WAITED + $1))
  [ -z "$LOCK_HOLDER" ] || return 0
  local pid
  # fuser prints the PIDs on stdout and the path on stderr; the lowest PID is the flock
  # parent, its children inherit the descriptor.
  pid=$(fuser "$LOCK" 2>/dev/null | tr -s ' ' '\n' | grep -m1 '[0-9]') || return 0
  LOCK_HOLDER=$(ps -o etimes=,args= -p "$pid" 2>/dev/null | tr -s ' ' | tr -d '"' | cut -c1-120)
  say "lock held by pid $pid (etimes, command): $LOCK_HOLDER"
}
# shellcheck disable=SC2317,SC2329  # both are reached through the EXIT trap below
phase() { # seconds between two stamps, or empty when the later one was never reached
  [ -n "$2" ] && [ -n "$1" ] && echo $(($2 - $1))
}
# shellcheck disable=SC2317,SC2329
emit_landing_annotation() {
  local rc=$?
  local verdict="${LAND_VERDICT:-aborted}"
  logger -t landing-annotation \
    "event=landing pr=${PR:-unknown} sha=${MERGE_SHA:0:8} verdict=${verdict} exit=${rc}" \
    "wait_merge=$(phase "$T_START" "$T_MERGED") wait_ci=$(phase "$T_MERGED" "$T_CI")" \
    "tick=$(phase "$T_CI" "$T_TICK") deploy=$(phase "$T_TICK" "$T_DEPLOY")" \
    "total=$((SECONDS - T_START)) tags=${TAGS_LABEL:-none}" \
    "lock=${LOCK_WAITED} holder=\"${LOCK_HOLDER}\"" \
    2>/dev/null || true
}
trap emit_landing_annotation EXIT

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
    --await-merge)
      AWAIT_MERGE=1
      shift
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

if [ "$AWAIT_MERGE" -eq 1 ]; then
  echo "== 0/6  waiting for PR #$PR to merge (auto-merge or the merge queue)"
  waited=0
  conflicting=0
  while :; do
    pr_state=$(gh pr view "$PR" --json state,mergeable,headRefOid \
      --jq '.state + " " + .mergeable + " " + .headRefOid') ||
      die "could not read PR #$PR" 1
    read -r state mergeable head_sha <<<"$pr_state"
    case "$state" in
      MERGED) break ;;
      CLOSED) die "PR #$PR was closed without merging — nothing to land" 1 ;;
    esac
    # A PR that goes CONFLICTING after `gh pr merge --auto` was armed never merges, and
    # nothing on the PR says so — with several sessions landing at once, another merge moving
    # master under an open PR is the ordinary way it happens. Without this the landing sat out
    # the whole 2700s budget and reported merge-timeout, which reads as "nobody merged it"
    # rather than "it cannot be merged".
    # Only CONFLICTING may bail — GitHub computes mergeability asynchronously and serves
    # UNKNOWN until it settles (PR #657 read UNKNOWN on a live open PR, 2026-09-02), so
    # bailing on anything-but-MERGEABLE would abort every landing that polled too early.
    # Two consecutive polls, because master moving under the PR flips the field for one
    # poll while GitHub recomputes.
    if [ "$mergeable" = "CONFLICTING" ]; then
      conflicting=$((conflicting + 1))
      if [ "$conflicting" -ge 2 ]; then
        die "PR #$PR conflicts with $BRANCH — rebase it, re-arm \`gh pr merge --squash --auto\`, then re-run this" 1 merge-conflict
      fi
    else
      conflicting=0
    fi
    # The other way an armed auto-merge never fires: the PR's own CI goes red. GitHub says
    # only `mergeStateStatus: BLOCKED`, which is the same word it uses while the checks are
    # still running, so the state fields alone cannot tell "about to merge" from "never
    # will". This repo's ruleset requires status checks and signatures and NO review, so a
    # BLOCKED PR here is always about checks — there is no human-review wait to preserve.
    #
    # await_ci.py owns the CI verdict, as it does for master CI after the merge; `--timeout 0`
    # makes it one-shot. Asking it every poll needs no grace period: it answers `pending`
    # (exit 75) until a required run registers, which is the grace period, derived rather
    # than guessed.
    #
    # Only exit 1 bails, and the asymmetry is deliberate. await_ci.py requires just `prek`,
    # narrower than the ruleset's three contexts, so a red prek means the ruleset can never
    # let this PR merge, while a green prek means only "keep waiting" — never "it will
    # merge". Do not widen the required set here to match the ruleset: the narrow set is
    # what makes the red half sound.
    if [ -n "$head_sha" ] && [ "$head_sha" != "null" ]; then
      ci_line=$(uv run python scripts/deploy_tools/await_ci.py --timeout 0 "$head_sha" 2>&1)
      ci_rc=$?
      if [ "$ci_rc" -eq 1 ]; then
        die "PR #$PR cannot merge — its own CI is red ($ci_line); fix it, push, and re-run this" 1 pr-ci-red
      fi
    fi
    if [ "$waited" -ge "$MERGE_TIMEOUT" ]; then
      die "PR #$PR still $state after ${MERGE_TIMEOUT}s — not being merged; look at its checks or the queue" 75 merge-timeout
    fi
    sleep "$MERGE_POLL"
    waited=$((waited + MERGE_POLL))
  done
  say "merged after ${waited}s"
fi

T_MERGED=$SECONDS
echo "== 1/6  resolving PR #$PR"
MERGE_SHA=$(gh pr view "$PR" --json mergeCommit --jq '.mergeCommit.oid') ||
  die "could not read PR #$PR" 1
if [ -z "$MERGE_SHA" ] || [ "$MERGE_SHA" = "null" ]; then
  die "PR #$PR has no merge commit — is it merged?" 1
fi
say "merge commit $MERGE_SHA"
git fetch -q origin "$BRANCH" || die "could not fetch origin/$BRANCH" 1

# What this PR reaches, read from its file list BEFORE any wait. A PR that reaches no service
# tag, no plane a hand applies and nothing the tick applies itself has nothing to wait for:
# the deployer fast-forwards it on its own tick, and CI on the merge commit is the deployer's
# gate, not this landing's. Sixteen of the 45 landings before 2026-09-02 ended
# nothing-to-deploy after a median seven minutes of PR CI plus master CI.
PLANE=''
SELF_APPLIED=''
NEEDS_DIFF=''
if [ -z "$TAGS" ]; then
  pr_json=$(gh pr view "$PR" --json files,changedFiles) || die "could not read PR files" 1
  # What a deploy tag cannot reach. deploy.yml is a containers_list loop, so a setup-plane
  # change needs initial_setup.yml and derives no tag at all — which land.sh used to report
  # as nothing-to-deploy. A shared k8s role (manifests, volume-claim, …) has no entry in that
  # list either, so it needs a full deploy. Computed whether or not tags were derived: a PR
  # can touch a deployable role AND one of those planes, and then the deploy succeeds while
  # half the change is unapplied, under a `settled` verdict.
  PLANE=$(uv run python scripts/deploy_tools/land_tags.py --plane --json "$pr_json") ||
    die "plane classification failed" 1
  # `yes` when the tick applies part of this PR itself (a setup role initial_setup.yml
  # includes, or the deploy plane). Only then does the deployer's state after the tick
  # speak to THIS landing — for an ordinary service PR, `behind_since` is somebody else's
  # pending merge and says nothing about the services deploy.sh just rolled out.
  SELF_APPLIED=$(uv run python scripts/deploy_tools/land_tags.py --self-applied --json "$pr_json") ||
    die "self-applied classification failed" 1
  derived=$(uv run python scripts/deploy_tools/land_tags.py --json "$pr_json") ||
    die "tag derivation failed" 1
  source_kind=${derived%% *}
  TAGS=${derived#* }
  if [ "$source_kind" = "fallback" ]; then
    [ -n "$SINCE" ] ||
      die "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>"
    # The diff-based derivation reads `$SINCE...HEAD`, and HEAD is the primary checkout,
    # which the tick has not fast-forwarded yet. Derived in step 5 instead, after the tick.
    NEEDS_DIFF=1
    say "file list truncated; deriving from the diff since $SINCE after the tick"
  fi
fi

if [ -z "$TAGS" ] && [ -z "$PLANE" ] && [ -z "$SELF_APPLIED" ] && [ -z "$NEEDS_DIFF" ]; then
  LAND_VERDICT=nothing-to-deploy
  echo "VERDICT: nothing-to-deploy (PR #$PR touched no service; the deployer fast-forwards it on its next tick)"
  exit 0
fi

# Before waiting on anything. A _BROAD_MANUAL_PREFIXES change anywhere in the incoming range
# stops the tick fast-forwarding, which guarantees deploy.sh refuses as stale (exit 4) however
# green CI turns out. Landing PR #570 on 2026-08-29 spent ~6 minutes waiting for CI and then
# failed at step 4, with the blocker visible in the range before the wait began.
echo "== 2/6  pre-flight: can the tick cross what is incoming?"
uv run python scripts/deploy_tools/deploy_tags.py blockers "origin/$BRANCH"
pf_rc=$?
case "$pf_rc" in
  0) say "nothing in the way" ;;
  3)
    LAND_VERDICT=blocked
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
  1) die "master CI is RED on $MERGE_SHA — nothing deployed" 1 ci-red ;;
  75) die "no CI verdict inside ${CI_TIMEOUT}s — nothing deployed" 75 ci-timeout ;;
  *) die "await_ci failed (exit $ci_rc) — nothing deployed" 1 ;;
esac

T_CI=$SECONDS
echo "== 4/6  GitOps tick (fetch, ff-merge, deploy what is eligible)"
tick_rc=0
attempt=1
while [ "$attempt" -le "$LOCK_RETRIES" ]; do
  t_attempt=$SECONDS
  ./scripts/deploy_tools/gitops_tick.sh
  tick_rc=$?
  # 3 = lock contention: the unit's own `flock -w 180` gave up, so the tick fast-forwarded
  # NOTHING. Another session's deploy can hold the tree lock for twenty minutes, and a
  # landing that carried on from here left the primary checkout behind origin with every
  # later step reading that as "the tick deferred" (#723, 2026-09-01). Retried the way the
  # scoped deploy below retries its own exit 75; each attempt already waits 180s inside
  # the unit, so five of them cover the long deploy this was measured against.
  if [ "$tick_rc" -ne 3 ]; then
    break
  fi
  note_lock_contention $((SECONDS - t_attempt + LOCK_BACKOFF))
  say "tick skipped for lock contention (attempt $attempt/$LOCK_RETRIES); retrying in ${LOCK_BACKOFF}s"
  sleep "$LOCK_BACKOFF"
  attempt=$((attempt + 1))
done
# 75 = the wrapper stopped watching a run still in flight. Not a failure of the tick, and it
# leaves the ff-merge either done or retryable next tick, so carry on to the scoped deploy.
case "$tick_rc" in
  0 | 75) say "tick exit $tick_rc" ;;
  3) die "tick skipped for lock contention $LOCK_RETRIES times — nothing fast-forwarded" 75 lock-busy ;;
  *) die "gitops tick failed (exit $tick_rc)" 1 ;;
esac

# What the deployer itself did with this landing, read from its own state rather than
# inferred from the PR's paths. A setup-plane or deploy-plane change is applied BY THE TICK
# since 2026-08-29 (the deployer's own role since #719), so for a PR with no service tag the
# tick's apply IS the deploy — and the only evidence of it is the deployer's markers.
# `behind_since` non-empty means the tick did not cross origin (a newer merge whose CI is
# still running, most often); `hold_sha` non-empty means an apply failed and the tick is
# holding. Both files are written by the deployer after main() returns.
DEPLOYER_STATE=/var/lib/gitops-deploy
tick_state() {
  if [ -s "$DEPLOYER_STATE/hold_sha" ]; then
    echo held
  elif [ -s "$DEPLOYER_STATE/behind_since" ]; then
    echo behind
  else
    echo converged
  fi
}

T_TICK=$SECONDS
echo "== 5/6  deploying what the tick deferred"
if [ -n "$NEEDS_DIFF" ]; then
  say "deriving tags from the diff since $SINCE"
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

if [ -z "$TAGS" ]; then
  if [ -n "$PLANE" ]; then
    echo "  it needs applying by hand: $PLANE"
    LAND_VERDICT=needs-manual-apply
    echo "VERDICT: needs-manual-apply (PR #$PR reaches no service tag, but is not done)"
    exit 1
  fi
  if [ -z "$SELF_APPLIED" ]; then
    LAND_VERDICT=nothing-to-deploy
    echo "VERDICT: nothing-to-deploy (PR #$PR touched no service)"
    exit 0
  fi
  # No service tag, nothing owed to a hand, and the tick applies this PR itself (a setup
  # role initial_setup.yml includes, or the deploy plane). The deployer's own state is the
  # only evidence of whether it did.
  case "$(tick_state)" in
    held)
      echo "  the deployer is holding $(cat "$DEPLOYER_STATE/hold_sha"): its apply failed — see hold_plane and the gitops-deploy journal"
      LAND_VERDICT=deploy-failed
      echo "VERDICT: deploy-failed (PR #$PR — the tick's own apply failed and is held)"
      exit 1
      ;;
    behind)
      echo "  the tick did not fast-forward to origin (parked since: $(cat "$DEPLOYER_STATE/behind_since"))"
      echo "  Usually a newer merge whose CI is still running; the next tick crosses it. Nothing is wrong with this PR."
      LAND_VERDICT=deferred
      echo "VERDICT: deferred (PR #$PR — landed, not yet applied by the tick)"
      exit 75
      ;;
  esac
  LAND_VERDICT=settled
  echo "VERDICT: settled (PR #$PR, $MERGE_SHA — no service tag; the tick applied it and converged with origin)"
  exit 0
fi

TAGS_LABEL=$TAGS
deploy_rc=0
attempt=1
while [ "$attempt" -le "$LOCK_RETRIES" ]; do
  t_attempt=$SECONDS
  ./scripts/deploy.sh --tags "$TAGS"
  deploy_rc=$?
  # 75 = the git-tree lock stayed busy (the 10-min timer, or another session). Nothing was
  # deployed, so this is a resume point rather than a failure.
  if [ "$deploy_rc" -ne 75 ]; then
    break
  fi
  note_lock_contention $((SECONDS - t_attempt + LOCK_BACKOFF))
  say "deploy lock busy (attempt $attempt/$LOCK_RETRIES); retrying in ${LOCK_BACKOFF}s"
  sleep "$LOCK_BACKOFF"
  attempt=$((attempt + 1))
done

# 4 = the tree is behind origin/master. CLAUDE.md classes this a resume point: pull again,
# never --skip-staleness-check. It happens when someone merges during the CI wait, so the tick
# at step 4 had a newer tip than the pre-flight checked. Another tick fetches and crosses it.
#
# The tick crosses the new tip only once master CI is green ON THE TIP (the journal reads
# `origin <tip>: CI not finished — deferring`). Step 3 waited on this PR's own merge commit,
# so an immediate re-tick defers again and the second deploy.sh exits 4 again. Three landings
# on 2026-09-02 (#747, #754 and one between) each ended `deploy-failed (exit 4)` that way and
# were settled by hand with `await_ci.py <tip>` then a second land.sh. So: wait on the tip
# first, after the blockers check — a landing that can never cross must not wait 15 minutes
# before saying so. await_ci.py given the tip itself does not chase anything further unless
# that tip's run is cancelled by yet another merge, which is the same rule step 3 relies on.
#
# Bounded at STALE_RETRIES on purpose. A single retry covered one merge landing during the
# wait; a third merge landing during the tip wait moved the tip again and the retry's own
# deploy exited 4. Each pass re-runs the blockers check, so a broad-manual change still ends
# `blocked` on the pass that sees it rather than hiding behind a stalled landing.
STALE_RETRIES=3
stale_attempt=0
while [ "$deploy_rc" -eq 4 ] && [ "$stale_attempt" -lt "$STALE_RETRIES" ]; do
  stale_attempt=$((stale_attempt + 1))
  say "tree went stale mid-landing (someone merged during the wait); re-ticking ($stale_attempt/$STALE_RETRIES)"
  git fetch -q origin "$BRANCH" || die "could not fetch origin/$BRANCH" 1
  uv run python scripts/deploy_tools/deploy_tags.py blockers "origin/$BRANCH"
  retry_pf_rc=$?
  if [ "$retry_pf_rc" -eq 3 ]; then
    LAND_VERDICT=blocked
    echo "VERDICT: blocked (PR #$PR — a change needing a hand landed during the wait; see above)"
    exit 1
  fi
  TIP_SHA=$(git rev-parse "origin/$BRANCH") || die "could not read origin/$BRANCH" 1
  if [ "$TIP_SHA" != "$MERGE_SHA" ]; then
    say "waiting for master CI on the new tip $TIP_SHA (the tick defers until it is green)"
    t_tip_wait_start=$SECONDS
    uv run python scripts/deploy_tools/await_ci.py "$TIP_SHA" --timeout "$CI_TIMEOUT"
    tip_ci_rc=$?
    # This wait is CI time, not deploy time, but it runs after the T_TICK stamp. Shifting
    # T_CI and T_TICK forward by the seconds waited books it under wait_ci on the Landings
    # board (wait_ci = T_CI - T_MERGED grows, tick = T_TICK - T_CI is unchanged, deploy =
    # T_DEPLOY - T_TICK shrinks by the same amount) with no new field for the board to learn.
    tip_waited=$((SECONDS - t_tip_wait_start))
    T_CI=$((T_CI + tip_waited))
    T_TICK=$((T_TICK + tip_waited))
    case "$tip_ci_rc" in
      0) ;;
      1) die "master CI is RED on the tip $TIP_SHA — the tick cannot cross it; nothing deployed" 1 ci-red ;;
      75) die "no CI verdict on the tip $TIP_SHA inside ${CI_TIMEOUT}s — nothing deployed" 75 ci-timeout ;;
      *) die "await_ci failed on the tip (exit $tip_ci_rc) — nothing deployed" 1 ;;
    esac
  fi
  ./scripts/deploy_tools/gitops_tick.sh
  ./scripts/deploy.sh --tags "$TAGS"
  deploy_rc=$?
done

case "$deploy_rc" in
  0) ;;
  2)
    # A tag matched no service, so deploy.sh refused the WHOLE list and nothing was deployed
    # — including every valid service beside the bad tag. This read as `nothing-to-deploy`
    # and exit 0 until 2026-08-29, which is how PR #617 left 22 digest pins undeployed behind
    # a green verdict. The derivation is fixed upstream; this stays as the backstop, because
    # a tag list deploy.sh will not accept is a defect and never a finished landing.
    LAND_VERDICT=deploy-failed
    echo "VERDICT: deploy-failed (PR #$PR — a derived tag matched no service, so nothing deployed; tags: $TAGS)"
    exit 1
    ;;
  75) die "deploy lock stayed busy after $LOCK_RETRIES attempts — nothing deployed" 75 lock-busy ;;
  *)
    LAND_VERDICT=deploy-failed
    echo "VERDICT: deploy-failed (PR #$PR, exit $deploy_rc)"
    exit 1
    ;;
esac

T_DEPLOY=$SECONDS
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
    LAND_VERDICT=needs-manual-apply
    echo "VERDICT: needs-manual-apply (PR #$PR, $MERGE_SHA — services deployed, the plane above not)"
    exit 1
  fi
  # The services are deployed and healthy. When the same PR also carries a half the tick
  # applies itself, only the deployer's state says whether it did; for an ordinary service
  # PR that state is somebody else's business and is not consulted.
  [ -n "$SELF_APPLIED" ] && case "$(tick_state)" in
    held)
      echo "  services deployed, but the deployer is holding $(cat "$DEPLOYER_STATE/hold_sha"): its own apply failed — see hold_plane"
      LAND_VERDICT=deploy-failed
      echo "VERDICT: deploy-failed (PR #$PR, $MERGE_SHA — services deployed, the tick's apply is held)"
      exit 1
      ;;
    behind)
      echo "  services deployed, but the tick has not fast-forwarded to origin (parked since: $(cat "$DEPLOYER_STATE/behind_since"))"
      LAND_VERDICT=deferred
      echo "VERDICT: deferred (PR #$PR, $MERGE_SHA, tags: $TAGS — services deployed, the tick's half not yet)"
      exit 75
      ;;
  esac
  LAND_VERDICT=settled
  echo "VERDICT: settled (PR #$PR, $MERGE_SHA, tags: $TAGS)"
  exit 0
fi
LAND_VERDICT=unhealthy
echo "VERDICT: unhealthy (PR #$PR, $MERGE_SHA, tags: $TAGS)"
exit 1
