#!/bin/bash
# InstructionsLoaded hook — observability only. Appends one line per CLAUDE.md /
# .claude/rules file as it loads (which file, and why: session_start vs
# path_glob_match vs nested_traversal, plus the trigger file) to
# .claude/logs/instructions.log, so path-scoped rule loading can be verified.
#
# The Python is pure stdlib, so we run system python3 directly rather than routing
# through uv — InstructionsLoaded fires once per instruction file on the session-start
# critical path, so the uv-env reconcile isn't worth the latency. `; exit 0` guarantees
# the hook never surfaces an error (even if python3 were missing); it cannot block.
python3 "$(dirname "$(readlink -f "$0")")/log-instructions.py" 2>/dev/null
exit 0
