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
# Usage: scripts/deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
#   --check runs unlocked; a dry run writes nothing worth serializing.

set -u

LOCK=/var/lock/server-git-tree.lock
LOCK_WAIT=1500 # matches gitops-deploy's own end-to-end budget (TimeoutStartSec=25min)
LOCK_BUSY=75

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root" || exit 1

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
