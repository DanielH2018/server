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
#   --list-services prints every valid --tags value and exits.
#   --skip-tag-check deploys a tag this wrapper does not recognise.

set -u

LOCK=/var/lock/server-git-tree.lock
LOCK_WAIT=1500 # matches gitops-deploy's own end-to-end budget (TimeoutStartSec=25min)
LOCK_BUSY=75

# The checkout this session is working in, not the primary one — a session in a worktree
# has always deployed its own tree, and running the wrapper must not change that.
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root" || exit 1

# Ansible exits 0 on a tag that matches nothing, so a typo'd service name deploys
# nothing and reports success -- see scripts/deploy_tags.py for why the play behaves
# that way. Catch it here, before the lock is taken and before --check, since a dry run
# against a nonexistent tag is just as misleading. --skip-tag-check bypasses, and is
# stripped so it never reaches ansible-playbook.
args=()
tags=()
next_is_tags=0
skip_tag_check=0

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

flock -w "$LOCK_WAIT" -E "$LOCK_BUSY" "$LOCK" uv run ansible-playbook ansible/deploy.yml "$@"
status=$?

if [[ "$status" == "$LOCK_BUSY" ]]; then
    echo "deploy: could not take $LOCK after ${LOCK_WAIT}s -- nothing was deployed." >&2
    echo "  A deploy is already running. Likely holders: gitops-deploy.service" >&2
    echo "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron," >&2
    echo "  or another Claude session (uv run python scripts/prune_worktrees.py)." >&2
fi

exit "$status"
