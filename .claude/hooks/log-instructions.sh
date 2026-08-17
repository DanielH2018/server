#!/bin/bash
# InstructionsLoaded hook — observability only. Appends one line per CLAUDE.md /
# .claude/rules file as it loads (which file, and why: session_start vs
# path_glob_match vs nested_traversal, plus the trigger file) to
# .claude/logs/instructions.log, so path-scoped rule loading can be verified.
#
# Routed through uv like every other host-run Python here: one way to run Python
# on these hosts, and the system 3.12 is no longer a viable interpreter for these
# scripts. `--no-project` because uv resolves a project from the cwd and a hook's
# cwd is arbitrary. `2>/dev/null` + `exit 0` guarantee the hook never surfaces an
# error (even if uv were missing); it cannot block.
/home/ubuntu/.local/bin/uv run --no-project --no-python-downloads --python 3.14.6 \
  "$(dirname "$(readlink -f "$0")")/log-instructions.py" 2>/dev/null
exit 0
