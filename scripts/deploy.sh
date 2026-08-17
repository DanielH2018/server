#!/bin/bash
# Run an interactive Ansible deploy under the same lock the automated deployers take.
#
# gitops-deploy.service (every 30 min) and the weekly secret-rotate cron both serialize on
# /var/lock/server-git-tree.lock. A hand- or agent-run `ansible-playbook` took no lock at
# all, so it could interleave with either of them -- writing the same rendered tree and
# talking to the same cluster -- while several Claude sessions could do the same to each
# other.
#
# The wait is deliberately longer than gitops-deploy's own 180s: an interactive deploy
# should queue behind the unattended pipeline rather than give up. Note this does NOT
# protect the pipeline from a slow deploy in the other direction -- if this run holds the
# lock for more than 180s, the next gitops firing fails its unit and raises a Discord alert.
# That alert is accurate (a deploy really was in progress) and the timer retries 30 minutes
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
#   --changed [<ref>] deploys every service touched vs <ref> (default origin/master) instead of
#     a hand-picked --tags. Resolves to --tags under the hood (scripts/deploy_tags.py changed),
#     so the derived list still goes through the same lock and tag validation below, and prints
#     what it derived before doing anything else. Refuses (exit 3, nothing touched) on a broad
#     change — shared templates/inventory/setup-plane paths that don't map to one service.

set -u

LOCK=/var/lock/server-git-tree.lock
LOCK_WAIT=1500 # matches gitops-deploy's own end-to-end budget (TimeoutStartSec=25min)
LOCK_BUSY=75

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
    derived_tags=$(uv run python scripts/deploy_tags.py changed "$changed_ref")
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
# nothing and reports success -- see scripts/deploy_tags.py for why the play behaves
# that way. Catch it here, before the lock is taken and before --check, since a dry run
# against a nonexistent tag is just as misleading. --skip-tag-check bypasses, and is
# stripped so it never reaches ansible-playbook.
args=()
tags=()
next_is_tags=0
skip_tag_check=0
dry_run=0

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
        --dry-run)
            # Translated, not passed through: ansible-playbook has no --dry-run of its own
            # (--check is the Ansible-level one, and it is a different mode entirely).
            dry_run=1
            args+=(-e k8s_dry_run=true)
            ;;
        --list-services)
            exec uv run python scripts/deploy_tags.py list
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
    if ! uv run python scripts/deploy_tags.py validate "${split_tags[@]}"; then
        exit 2
    fi
fi

set -- "${args[@]}"

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

flock -w "$LOCK_WAIT" -E "$LOCK_BUSY" "$LOCK" uv run ansible-playbook ansible/deploy.yml "$@"
status=$?

if [[ "$status" == "$LOCK_BUSY" ]]; then
    echo "deploy: could not take $LOCK after ${LOCK_WAIT}s -- nothing was deployed." >&2
    echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
    echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
    echo "  or another Claude session (uv run python scripts/prune_worktrees.py)." >&2
fi

exit "$status"
