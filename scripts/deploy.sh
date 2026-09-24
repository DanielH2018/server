#!/bin/bash
# deploy.sh — the entry point every doc, skill, hook and consumer names; it execs deploy_run.py.
#
# The implementation is scripts/deploy_tools/deploy_run.py, whose docstring carries the usage
# and the exit codes, and deploy_locked.sh beside it, until #2412's port finishes
# (docs/deploy-sh-python-port.md).
#
# No `cd`, unlike land.sh: the run deploys the checkout containing the CALLER's working
# directory, and a session in a worktree has always deployed its own tree. `--project` makes
# uv resolve its environment from THIS script's checkout wherever the caller stands, so the
# Python that runs is the same release as this shim.
set -u
root=$(dirname "$(dirname "$(readlink -f "$0")")")
exec uv run --project "$root" python "$root/scripts/deploy_tools/deploy_run.py" "$@"
