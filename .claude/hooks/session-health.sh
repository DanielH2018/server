#!/bin/bash
# SessionStart hook — print a homelab health banner (unhealthy/restarting
# containers + down Prometheus targets) when a session opens in this repo,
# plus the other Claude sessions currently working this repo and the paths
# each has changed. Silent when all-green and no other session is open.
# See session-health.py for the full contract.
#
# The script is stdlib-only (it shells out to docker/uv as subprocesses), so we
# run system python3 directly. `2>/dev/null` + `exit 0` guarantee the hook can
# never surface an error or block session start.
python3 "$(dirname "$(readlink -f "$0")")/session-health.py" 2>/dev/null
exit 0
