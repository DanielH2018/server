#!/bin/bash
# gen-hooks: library
#   reason: kept for a session whose settings.json predates the switch to `run-hook.sh fanout-stop` (#3278); registered from fanout-stop.py
# Stop hook — in a headless fan-out worktree (one holding .fanout/brief.md), block a
# stop whose final message names neither a PR nor a blocker, at most three times per
# batch. Silent everywhere else. See fanout-stop.py for the full contract.
#
# Routed through uv like every other host-run Python here. `2>/dev/null` + `exit 0`
# mean a broken hook lets the session stop rather than surfacing an error; the
# worst case is the pre-hook behaviour, which `fanout_place.py status` reports as
# `no-pr` rather than `done`.
/home/ubuntu/.local/bin/uv run --no-project --no-python-downloads --python 3.14.6 \
  "$(dirname "$(readlink -f "$0")")/fanout-stop.py" 2>/dev/null
exit 0
