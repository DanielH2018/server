#!/usr/bin/env bash
# Fast-forward the claude agent user's clone to origin's master, when it is a clean master.
#
# The hook shim and the homelab-ui MCP launcher run from the clone's main working tree, so a
# clone left behind runs old hooks and an old launcher (#4067, #4099). The claude_code role runs
# this on each apply, and claude-clone-sync.timer runs it between applies: a merge that touches
# only scripts/diagnostics/ or .claude/hooks/ applies no role, so the apply alone never moved
# the clone for it (#4162). One script for both, so the two never disagree on what is safe to
# move.
#
# Runs as the agent user, with HOME set to its home. Prints one line on stdout:
#   skipped: <reason>   the clone is off master or has uncommitted changes: the agent's work
#   up to date at <sha>
#   advanced <old>..<new>
# Exits non-zero only when a git, uv or ansible-galaxy step fails, with its stderr.
#
# Usage: claude-clone-sync.sh <clone-dir>
set -euo pipefail

clone=${1:?usage: claude-clone-sync.sh <clone-dir>}
export LC_ALL=C
cd "$clone"

# The apply and the timer run this same script; the second to start skips rather than racing
# the first for git's ref locks.
exec 9>"$(git rev-parse --git-dir)/claude-clone-sync.lock"
if ! flock -n 9; then
  echo "skipped: another claude-clone-sync run holds the lock"
  exit 0
fi

status=$(git status --porcelain=v2 --branch --untracked-files=no)
if ! grep -qx '# branch.head master' <<<"$status"; then
  echo "skipped: $clone is not on master"
  exit 0
fi
# Every porcelain v2 line that is not a `# ` header names a changed tracked file.
if grep -q '^[^#]' <<<"$status"; then
  echo "skipped: $clone has uncommitted changes"
  exit 0
fi

before=$(git rev-parse HEAD)
git pull --quiet --ff-only origin master
after=$(git rev-parse HEAD)
if [[ $before == "$after" ]]; then
  echo "up to date at ${after:0:12}"
  exit 0
fi
echo "advanced ${before:0:12}..${after:0:12}"

# The hook shim runs every guard with `uv run --no-sync`, so a pull that moves the lock and
# leaves the venv behind leaves guards that cannot import, and each falls back to `ask` (#3513).
if ! git diff --quiet "$before" "$after" -- uv.lock pyproject.toml; then
  "$HOME/.local/bin/uv" sync --frozen
fi
# The login profile points ANSIBLE_COLLECTIONS_PATH at the clone's collections (#4108).
if ! git diff --quiet "$before" "$after" -- ansible/requirements.yml; then
  .venv/bin/ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections
fi
