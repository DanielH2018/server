#!/usr/bin/env bash
#
# claude-remote-control.sh — launch (or reattach to) a detached tmux session
# running the Claude Code remote-control server.
#
# Remote Control lets you drive local Claude Code sessions from claude.ai/code
# or the Claude mobile app. It is a long-lived server process, so we keep it in
# a detached tmux session that survives SSH disconnects. Re-running this script
# attaches to the existing session instead of spawning a second server.
#
# Defaults match the homelab setup; override any of them via environment vars:
#   RC_DIR=/path RC_CAPACITY=10 RC_SPAWN=session ./scripts/claude-remote-control.sh
#
set -euo pipefail

SESSION="${RC_SESSION:-claude}"
WORKDIR="${RC_DIR:-$HOME/server}"

RC_NAME="${RC_NAME:-Server}"
RC_SPAWN="${RC_SPAWN:-worktree}"
RC_CAPACITY="${RC_CAPACITY:-5}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already running."
else
  echo "Starting remote-control server in '$WORKDIR' (tmux session '$SESSION')..."
  # Create detached so the remote-control server is the session's window;
  # tmux runs this string via 'sh -c', so the inner quotes keep multi-word
  # values (e.g. a name with spaces) intact.
  tmux new-session -d -s "$SESSION" -c "$WORKDIR" \
    "claude remote-control --name '$RC_NAME' --spawn '$RC_SPAWN' --capacity '$RC_CAPACITY'"
fi

# Attach only when we have a terminal. Under cron / a non-interactive SSH there
# is no tty, and 'tmux attach' would die with "open terminal failed: not a
# terminal" — so just report how to attach later.
if [ -t 0 ] && [ -t 1 ]; then
  exec tmux attach-session -t "$SESSION"
else
  echo "Non-interactive shell — attach later with: tmux attach -t $SESSION"
fi
