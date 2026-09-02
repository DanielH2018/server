#!/bin/bash
# Run an interactive Ansible deploy under the same lock the automated deployers take.
#
# gitops-deploy.service (every 10 min) and the weekly secret-rotate cron both serialize on
# /var/lock/server-git-tree.lock. A hand- or agent-run `ansible-playbook` took no lock at
# all, so it could interleave with either of them -- writing the same rendered tree and
# talking to the same cluster -- while several Claude sessions could do the same to each
# other.
#
# The wait is deliberately longer than gitops-deploy's own 180s: an interactive deploy
# should queue behind the unattended pipeline rather than give up. Note this does NOT
# protect the pipeline from a slow deploy in the other direction -- if this run holds the
# lock for more than 180s, the next gitops firing fails its unit and raises a Discord alert.
# That alert is accurate (a deploy really was in progress) and the timer retries 10 minutes
# later, so it is left as a true signal rather than suppressed.
#
# A `-e target=daniel-pi` deploy takes the lock too, even though it writes to the Pi: what
# the lock guards is the local git tree every deploy reads its templates from, and
# gitops-deploy rewrites that tree with a `git pull` mid-run.
#
# Usage: scripts/deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
#   --check runs unlocked; a dry run writes nothing worth serializing.
#   --dry-run validates the k8s manifests against the live API server without applying them
#     (-e k8s_dry_run=true). Also unlocked, and for a stronger reason than --check: it mutates
#     nothing at all, on the cluster or on the staging tree. See k8s_dry_run in
#     inventory/group_vars/all.yml, including the 19 roles it refuses to cover.
#   --list-services prints every valid --tags value and exits.
#   --skip-tag-check deploys a tag this wrapper does not recognise.
#   --skip-staleness-check deploys from a tree behind origin/master. Refused by default
#     (exit 4, nothing deployed): a stale tree renders stale templates and reverts live
#     config, and every repo-side check reads green while it does. Being *ahead* of master
#     is normal branch work and is never refused.
#   --changed [<ref>] deploys every service touched vs <ref> (default origin/master) instead of
#     a hand-picked --tags. Resolves to --tags under the hood (scripts/deploy_tools/deploy_tags.py changed),
#     so the derived list still goes through the same lock and tag validation below, and prints
#     what it derived before doing anything else. Refuses (exit 3, nothing touched) on a broad
#     change — shared templates/inventory/setup-plane paths that don't map to one service.
#   --detach backgrounds the ansible-playbook run (the ~83% of a deploy that is waiting on
#     rollout/stabilisation) and returns immediately. Tag validation, the staleness check, and
#     the lock are still evaluated in THIS process before it returns, so exit 2/4 land exactly
#     as they do today; lock contention (exit 75) is checked non-blocking instead of queued for
#     LOCK_WAIT, since waiting 45 minutes before returning would defeat the point of detaching —
#     it fails fast and asks you to retry rather than sitting on the terminal. Output goes to a
#     log file (path printed on return); on completion it posts to the gitops-deploy Discord
#     webhook, gated on `probe.py health <svc>` for every deployed tag that supports it.
#     Meaningless combined with --check or --dry-run (both already return immediately without
#     touching the lock) — refused with a nonzero exit rather than silently ignored.

set -u

# Ansible's cli/__init__.py refuses to start on a non-blocking stdout or stderr
# ("ERROR: Ansible requires blocking IO on stdin/stdout/stderr"), and Claude Code's Bash
# tool hands its child a regular file with O_NONBLOCK set. The .claude/hooks/uv-python.sh
# fixup cannot reach the `uv run ansible-playbook` inside this script — it only rewrites
# the command text a session types — so the restore is repeated here. O_NONBLOCK lives on
# the open file description that fork/exec shares, so clearing it from this child clears
# it for the ansible run below; on a terminal or under systemd the flag is already clear
# and this is a no-op. `3>&2` hands the child the real stderr past the `2>/dev/null` that
# keeps it quiet — without it the flag would be cleared on /dev/null instead.
python3 -c 'import os; [os.set_blocking(f, True) for f in (0, 1, 3)]' 3>&2 2>/dev/null || true

LOCK=/var/lock/server-git-tree.lock
# Covers gitops-deploy's worst-case hold of 2940s (STAGING_GATE_TIMEOUT_S 600 +
# STAGING_EXPECT_TIMEOUT_S 120 + K8S_DEPLOY_TIMEOUT_S 900 + K8S_ROLLBACK_TIMEOUT_S 1320), not its
# TimeoutStartSec. Was 1500 from d1a5b6c9 until 2026-08-23, when the unit's TimeoutStartSec really
# was 25min; it then went 25 -> 35 -> 45min and this value was left behind, so a deploy launched
# during a pathological gitops run gave up having deployed nothing while the run it was queued
# behind was still legitimately working. Derived from the same role defaults the deployer reads
# and pinned by test_deploy_sh_lock_wait_clears_the_deployers_worst_case_hold, so raising any of
# the four fails that test rather than silently shortening this wait again.
LOCK_WAIT=3000
LOCK_BUSY=75

# Record a successful deploy where Grafana can draw it as a dashboard annotation.
#
# WHY A LOG LINE and not a POST to Grafana's /api/annotations. Grafana has no hostPort and no
# pinned ClusterIP, and this script is a HOST process, so calling into the cluster would mean
# either pinning a fourth address or routing through Traefik with a standing write credential.
# Neither is needed: promtail already tails /var/log/syslog into loki-homelab on both nodes (the
# same path the host crons' `status=down` lines take to the alert-history board), and Grafana
# already reads that Loki by Service DNS. So the deployer writes locally and the cluster reads —
# no address, no credential, no new component.
#
# It also puts the record somewhere that SURVIVES. grafana-data is on longhorn-nobackup, so
# annotations stored in Grafana's own database have no offsite copy; reconstructing them from
# Loki matches the existing decision that Grafana holds nothing worth backing up.
#
# Fire-and-forget by construction: `|| true` and a discarded stderr. An annotation is a
# convenience, and a deploy that actually succeeded must never report failure because logging
# it did not.
emit_deploy_annotation() {
    local status="$1"
    [[ "$status" == 0 ]] || return 0

    local label
    label=$(
        IFS=,
        echo "${tags[*]:-full}"
    )
    # logfmt, so a Grafana annotation query can pull `services` out as the annotation text
    # rather than showing the whole raw line.
    logger -t deploy-annotation \
        "event=deploy services=${label} sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown) result=ok" \
        2>/dev/null || true
}

# The checkout this session is working in, not the primary one — a session in a worktree
# has always deployed its own tree, and running the wrapper must not change that.
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root" || exit 1

# --changed [<ref>] is resolved to --tags "<derived>" right here, before anything else looks at
# "$@" — everything below (locking, --check/--dry-run detection, tag validation) then runs
# exactly the --tags path it always has, unchanged. Pulled out as its own pass because the ref
# is an OPTIONAL positional: the single-pass case-statement loop below (which handles --tags'
# own required argument via next_is_tags) can't tell "no ref given" from "the next flag" without
# look-ahead, so --changed gets one.
raw_args=("$@")
filtered_args=()
i=0
n=${#raw_args[@]}
changed_requested=0
changed_ref="origin/master"
while [[ "$i" -lt "$n" ]]; do
    a="${raw_args[$i]}"
    if [[ "$a" == "--changed" ]]; then
        changed_requested=1
        i=$((i + 1))
        if [[ "$i" -lt "$n" && "${raw_args[$i]}" != -* ]]; then
            changed_ref="${raw_args[$i]}"
            i=$((i + 1))
        fi
        continue
    fi
    filtered_args+=("$a")
    i=$((i + 1))
done
set -- "${filtered_args[@]}"

if [[ "$changed_requested" == 1 ]]; then
    derived_tags=$(uv run python scripts/deploy_tools/deploy_tags.py changed "$changed_ref")
    status=$?
    if [[ "$status" != 0 ]]; then
        exit "$status"
    fi
    if [[ -z "$derived_tags" ]]; then
        exit 0
    fi
    set -- "$@" --tags "$derived_tags"
fi

# Ansible exits 0 on a tag that matches nothing, so a typo'd service name deploys
# nothing and reports success -- see scripts/deploy_tools/deploy_tags.py for why the play behaves
# that way. Catch it here, before the lock is taken and before --check, since a dry run
# against a nonexistent tag is just as misleading. --skip-tag-check bypasses, and is
# stripped so it never reaches ansible-playbook.
args=()
tags=()
next_is_tags=0
skip_tag_check=0
skip_staleness_check=0
dry_run=0
detach=0

for arg in "$@"; do
    if [[ "$next_is_tags" == 1 ]]; then
        next_is_tags=0
        tags+=("$arg")
        args+=("$arg")
        continue
    fi
    case "$arg" in
        --skip-tag-check)
            skip_tag_check=1
            ;;
        --skip-staleness-check)
            skip_staleness_check=1
            ;;
        --detach)
            detach=1
            ;;
        --dry-run)
            # Translated, not passed through: ansible-playbook has no --dry-run of its own
            # (--check is the Ansible-level one, and it is a different mode entirely).
            dry_run=1
            args+=(-e k8s_dry_run=true)
            ;;
        --list-services)
            exec uv run python scripts/deploy_tools/deploy_tags.py list
            ;;
        --tags | -t)
            next_is_tags=1
            args+=("$arg")
            ;;
        --tags=*)
            tags+=("${arg#--tags=}")
            args+=("$arg")
            ;;
        *)
            args+=("$arg")
            ;;
    esac
done

if [[ "$skip_tag_check" == 0 && ${#tags[@]} -gt 0 ]]; then
    # Ansible accepts comma-separated tags in one argument (--tags "a,b"), so split
    # each argument before checking. Done with an explicit IFS swap around the
    # expansion rather than a prefix assignment on `read`, whose effect on a
    # herestring expansion is not worth relying on.
    split_tags=()
    old_ifs=$IFS
    for tag_arg in "${tags[@]}"; do
        IFS=','
        # shellcheck disable=SC2086  # unquoted on purpose: this IS the comma split
        for tag in $tag_arg; do
            split_tags+=("$tag")
        done
        IFS=$old_ifs
    done
    if ! uv run python scripts/deploy_tools/deploy_tags.py validate "${split_tags[@]}"; then
        exit 2
    fi
fi

set -- "${args[@]}"

# --detach + --check/--dry-run is meaningless: both of those already return immediately without
# touching the lock, so there is nothing to background. Checked here, right after args are known
# and before the (comparatively slow) staleness check, so a nonsensical combination fails fast
# rather than doing something surprising.
check_requested=0
for arg in "$@"; do
    if [[ "$arg" == "--check" ]]; then
        check_requested=1
        break
    fi
done
if [[ "$detach" == 1 && ( "$check_requested" == 1 || "$dry_run" == 1 ) ]]; then
    echo "deploy: --detach with --check or --dry-run is meaningless -- both already return" >&2
    echo "  immediately without touching the lock, so there is nothing to background." >&2
    exit 64
fi

# The fact cache is shared by host across every worktree on this machine, and it pins the
# interpreter of whichever session gathered facts first. A cache naming a worktree that is gone
# fails EVERY deploy at Gathering Facts for the full 7200s TTL, with an error that names a module
# rather than the cache -- and it does so AFTER the ~9 minute wait on the lock below, so the run
# looks like it built for ten minutes and then died having done nothing. Clearing costs one
# re-gather, so this clears rather than refuses. Runs before --check and --dry-run too: a dry run
# gathers facts like any other run, which is how the cache gets re-poisoned in the first place.
#
# DECIDED: this preflight fails OPEN. It is a remediation, not a verdict -- if it cannot clear the
# cache, the deploy proceeds and dies at Gathering Facts exactly as it does today, except now with
# this script's stderr naming the cache directly above the misleading module error. Blocking every
# deploy on a bug in a cache-cleaner would be a worse failure than the one it prevents.
uv run python scripts/deploy_tools/fact_cache_guard.py --clear || true

# A tree behind origin/master renders stale templates and reverts live config for the roles
# it targets, while every repo-side check still reads green -- the stale tree is consistent
# with itself. Measured 2026-08-19; see scripts/deploy_tools/deploy_staleness.py. This runs before --check
# and --dry-run too: a green dry run against a stale tree is the misleading signal itself.
if [[ "$skip_staleness_check" == 0 ]]; then
    if ! uv run python scripts/deploy_tools/deploy_staleness.py; then
        exit 4
    fi
fi

for arg in "$@"; do
    if [[ "$arg" == "--check" ]]; then
        exec uv run ansible-playbook ansible/deploy.yml "$@"
    fi
done

# Same reasoning as --check, and a stronger case for it: a k8s dry run renders to a temp dir it
# then deletes and applies with --dry-run=server, so it writes neither the cluster nor the
# staging tree. Nothing for the lock to serialize against.
if [[ "$dry_run" == 1 ]]; then
    exec uv run ansible-playbook ansible/deploy.yml "$@"
fi

if [[ "$detach" == 1 ]]; then
    # The lock is still taken HERE, synchronously -- only the ansible-playbook run itself (the
    # ~83% of a deploy spent waiting on rollout/stabilisation) moves to the background. Waiting
    # up to LOCK_WAIT (45min) before returning would defeat the point of --detach, so contention
    # is checked non-blocking: exit 75 means "try again shortly", not "waited 45 minutes then
    # gave up" the way it does without --detach.
    log_dir=/tmp/homelab-deploy-logs
    mkdir -p "$log_dir"
    health_tags=()
    if [[ ${#tags[@]} -gt 0 ]]; then
        old_ifs=$IFS
        for tag_arg in "${tags[@]}"; do
            IFS=','
            # shellcheck disable=SC2086  # unquoted on purpose: this IS the comma split
            for tag in $tag_arg; do
                health_tags+=("$tag")
            done
            IFS=$old_ifs
        done
    fi
    tag_label=$(
        IFS=,
        echo "${health_tags[*]:-full}"
    )
    log="$log_dir/deploy-${tag_label//[^A-Za-z0-9_.,-]/_}-$(date +%Y%m%d-%H%M%S)-$$.log"

    exec {lockfd}>"$LOCK"
    if ! flock -n "$lockfd"; then
        echo "deploy --detach: could not take $LOCK right now -- nothing was deployed." >&2
        echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
        echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
        echo "  or another Claude session (uv run python scripts/dev/prune_worktrees.py)." >&2
        echo "  --detach fails fast on contention rather than queuing for ${LOCK_WAIT}s --" >&2
        echo "  retry shortly, or drop --detach to queue normally." >&2
        exit "$LOCK_BUSY"
    fi

    (
        uv run ansible-playbook ansible/deploy.yml "$@" >"$log" 2>&1
        run_status=$?
        flock -u "$lockfd"
        exec {lockfd}>&-
        # Annotated from inside the subshell, where the run actually finished — the parent
        # returned at exit 0 the moment it backgrounded this, long before there was anything
        # to record.
        emit_deploy_annotation "$run_status"
        # shellcheck disable=SC2094  # false positive: the notifier only receives $log as a
        # path string (to mention in its Discord post) and never opens it itself -- the only
        # actual writer of the file is this append redirect.
        uv run python scripts/deploy_tools/deploy_detach_notify.py \
            --status "$run_status" \
            --log "$log" \
            --tags "$(
                IFS=,
                echo "${health_tags[*]}"
            )" \
            >>"$log" 2>&1
    ) &
    bg_pid=$!
    disown "$bg_pid" 2>/dev/null || true
    exec {lockfd}>&-

    echo "deploy --detach: running in background (pid $bg_pid)."
    echo "  log:  $log"
    echo "  tail: tail -f $log"
    echo "  Posts to the gitops-deploy Discord webhook when it settles, gated on" \
        "'probe.py health <svc>' for every deployed tag that supports it."
    exit 0
fi

flock -w "$LOCK_WAIT" -E "$LOCK_BUSY" "$LOCK" uv run ansible-playbook ansible/deploy.yml "$@"
status=$?

# After the lock is released and only on success. `--check` and `--dry-run` never reach here —
# both exec out well above — so a mode that changes nothing cannot annotate as though it had.
emit_deploy_annotation "$status"

if [[ "$status" == "$LOCK_BUSY" ]]; then
    echo "deploy: could not take $LOCK after ${LOCK_WAIT}s -- nothing was deployed." >&2
    echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
    echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
    echo "  or another Claude session (uv run python scripts/dev/prune_worktrees.py)." >&2
fi

exit "$status"
