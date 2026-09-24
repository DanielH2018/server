#!/bin/bash
# gen-hooks: register
#   event: PreToolUse
#   matcher: Bash
#   timeout: 15
#   order: 10
# PreToolUse(Bash) hook — the one process that runs all four Bash arms: the
# protected-file guard, the land.sh nudge, the footgun guard and the nested-docs
# injector. Each used to be its own hook with its own `uv run` start; issue #2394 merged
# them because all import the same two modules. The read-only classifier that was a
# fifth arm moved to the dotfiles `claude_guard` package (dotfiles #628). `bash-pretool.py`'s
# docstring owns the merge rule and the per-arm failure posture.
#
# Routed through uv so the project-pinned interpreter runs (not the system python3, which
# cannot parse this repo); --no-sync skips the env reconcile to stay fast on the hot path.
# `exec` preserves the hook's stdin JSON. No output -> normal permission flow.
#
# The timeout is 15s, the largest of the shims it replaced (block-protected-bash's): one
# process now does every arm's work, so the budget has to cover the slowest of them.
#
# DECIDED: the `cd` arm asks; every other failure stays fail-open. Issue #1014 made all
# three failure paths fail-open so that a broken guard does not brick every tool call, and
# gave the `cd` arm the stderr line the other two already had. An `ask` is a prompt, not a
# brick: the operator reads why a DENY guard could not run and decides once, where a bare
# `exit 0` disarmed it in silence (#2171). `uv` missing or the .py missing still exit
# non-zero with their own stderr line — a failed `exec` ends a non-interactive shell, so no
# `ask` can follow it. #2394 made one shim carry a posture that used to be split: three of
# the five arms asked on a failed `cd` and two stayed silent, and a single process can only
# do one thing, so it asks and the reason names all three guards that did not run. The two
# silent arms cost a missed approval and a missed doc injection, which is a prompt and a
# re-read, not a bypass.
cd /home/ubuntu/server || {
  echo "bash-pretool.sh: guards did not run (cd to /home/ubuntu/server failed) — asking instead of allowing" >&2
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"bash-pretool.sh: the guards did not run (cd to /home/ubuntu/server failed), so this call was checked by neither block-protected-bash, nudge-land-sh nor block-footguns. Review it yourself."}}'
  exit 0
}
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/bash-pretool.py"
