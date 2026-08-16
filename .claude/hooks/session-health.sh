#!/bin/bash
# SessionStart hook — print a homelab health banner (unhealthy/restarting
# containers + down Prometheus targets) when a session opens in this repo,
# plus the other Claude sessions currently working this repo and the paths
# each has changed. Silent when all-green and no other session is open.
# See session-health.py for the full contract.
#
# Routed through uv like every other host-run Python here: one way to run Python
# on these hosts, and the system 3.12 is no longer a viable interpreter for these
# scripts. `--no-project` because uv resolves a project from the cwd and a hook's
# cwd is arbitrary. `2>/dev/null` + `exit 0` guarantee the hook can never surface
# an error or block session start.
/home/ubuntu/.local/bin/uv run --no-project --no-python-downloads --python 3.14.6 \
  "$(dirname "$(readlink -f "$0")")/session-health.py" 2>/dev/null
exit 0
