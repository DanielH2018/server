#!/bin/bash
# gen-hooks: library
#   reason: kept for sessions started before bash-pretool.sh, which run this shim by path; delete once they have cycled (#2465)
# PreToolUse(Bash) hook — inject the nested CLAUDE.md / .claude/rules a Bash read of a
# role file would otherwise never load (issue #2125), once per session, logged to
# instructions.log as `bash_path_match`. Routed through uv so the project-pinned interpreter
# runs (not the system python3); --no-sync skips the env reconcile to stay fast on the
# per-command hot path. `exec` preserves the hook's stdin JSON. No output -> nothing to add.
# DECIDED: fail-open, not fail-closed, on all three ways this shim can break — a broken
# guard must not brick every tool call. `uv` missing or the .py missing already write their
# own line to stderr; only the `cd` arm was silent, so it now matches them. See issue #1014.
cd /home/ubuntu/server || { echo "inject-nested-docs.sh: hook did not run (cd to /home/ubuntu/server failed) — command allowed through" >&2; exit 0; }
exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
  "$(dirname "$(readlink -f "$0")")/inject-nested-docs.py"
