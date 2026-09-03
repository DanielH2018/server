#!/usr/bin/env bash
# land.sh — the entry point every doc, skill, hook and the renovate prompt names. The
# implementation is land.py and the land_lib package beside it; `land.sh --help` prints
# the contract this header used to carry: why one invocation, why the redirect, the exit
# codes, the verdicts. This exec exists so `./scripts/deploy_tools/land.sh …` keeps working
# unchanged and nudge-land-sh.py's escape (`"land.sh" in command`) still matches.
exec uv run python "$(dirname "$(readlink -f "$0")")/land.py" "$@"
