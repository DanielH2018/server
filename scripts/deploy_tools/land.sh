#!/usr/bin/env bash
# land.sh — the entry point every doc, skill and hook names; it execs land.py beside it.
#
# The implementation is land.py and the land_lib package beside it; `land.sh --help` prints
# the contract this header used to carry: why one invocation, why the redirect, the exit
# codes, the verdicts. This exec exists so `./scripts/deploy_tools/land.sh …` keeps working
# unchanged and nudge-land-sh.py's escape (`"land.sh" in command`) still matches.
#
# The cd is load-bearing: `uv run` resolves the project from its CALLER's working directory,
# so invoked from outside a checkout it builds an environment without the repo's dependencies
# and land.py dies at import (`ModuleNotFoundError: yaml`) before it can annotate anything.
# It is THIS checkout's root, not PRIMARY: the helpers land.py imports must be the same
# release as land.py itself (issue #851), while deploy_tags.py and deploy.sh keep their own
# `cwd=PRIMARY` in tools.py. The exec path is relative to that root rather than to `$0`,
# which after the cd would resolve against the new working directory.
cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1
exec uv run python scripts/deploy_tools/land.py "$@"
