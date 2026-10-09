#!/usr/bin/env bash
# List the files a pull request changes, and say whether its scoped CI run can be trusted.
#
# Every scoped job in .github/workflows/ci.yml calls this from its `id: changed` step, then
# makes its own decision from what it writes. The jobs differ only in that decision: the
# `hooks` job fails a PR whose merge changes nothing, and `ansible_lint` filters the list to
# ansible YAML. The fetch, the diff and the full-sweep rule are the same for every job, so they
# live here once.
#
# Inputs, from the environment:
#   BASE_REF          the PR's base branch (github.base_ref)
#   FETCH_TOKEN       a contents:read token (github.token)
#   RUNNER_TEMP       where the output files go
#   FULL_SWEEP_PATHS  optional; newline-separated paths whose change forces a full sweep
#
# Outputs, in $RUNNER_TEMP:
#   changed.txt       paths the PR adds or modifies, deletions excluded
#   deleted.txt       paths the PR deletes
#   full_sweep.txt    one line saying why the scoped list cannot be trusted; EMPTY when it can
#
# A deletion forces the full sweep: a PR that only deletes files yields an EMPTY changed list,
# and "skip everything" is the wrong answer there, because dropping a template changes what
# the render validators produce. A change to a FULL_SWEEP_PATHS entry forces it too: that file
# changes what a hook CHECKS rather than being a file it checks, so the scoped run would not
# see the change.
#
# It runs in the current directory's repository, which on the runner is the checkout of the
# PR's merge ref.
set -euo pipefail

# `--help` answers from any environment, ahead of every step below — the repo-wide convention
# `scripts/lib/tests/test_entry_points_answer_help.py` checks. The awk prints this
# file's leading comment block, which is the usage, with the `#` markers stripped.
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

: "${BASE_REF:?BASE_REF is unset}"
: "${FETCH_TOKEN:?FETCH_TOKEN is unset}"
: "${RUNNER_TEMP:?RUNNER_TEMP is unset}"

# The checkout runs with persist-credentials: false (zizmor artipacked), so a bare fetch has no
# credential and dies on "could not read Username". The token rides in a one-shot header the
# way actions/checkout itself sends it; nothing is written to .git/config.
git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$FETCH_TOKEN" | base64 -w0)" \
  fetch --no-tags origin "$BASE_REF"

# `A...B` (three dots) diffs against the merge base, so this is the PR's own change and not
# whatever else landed on the base meanwhile. --no-renames reports a rename as its D + A
# halves rather than one R entry, so the D trips the full-sweep rule.
base="origin/$BASE_REF...HEAD"
git diff --name-only --no-renames --diff-filter=D "$base" > "$RUNNER_TEMP/deleted.txt"
# --diff-filter=d (lowercase) drops deletions: a removed file is still "changed", but passing a
# path that no longer exists to a hook is a hook error, not a lint finding.
git diff --name-only --no-renames --diff-filter=d "$base" > "$RUNNER_TEMP/changed.txt"

# `-x` so `prek.toml` cannot match `docs/prek.toml.md`; the blank-line filter so a block
# scalar's trailing newline cannot become an empty pattern that matches everything. Each grep
# tolerates exit 1 (no match) and nothing else, so a read error still fails the step.
printf '%s\n' "${FULL_SWEEP_PATHS:-}" | { grep -v '^$' || [ $? -eq 1 ]; } > "$RUNNER_TEMP/triggers.txt"
mapfile -t triggered < <(grep -Fx -f "$RUNNER_TEMP/triggers.txt" "$RUNNER_TEMP/changed.txt" || [ $? -eq 1 ])

if [ -s "$RUNNER_TEMP/deleted.txt" ]; then
  echo "PR deletes $(wc -l < "$RUNNER_TEMP/deleted.txt") file(s)" > "$RUNNER_TEMP/full_sweep.txt"
elif [ "${#triggered[@]}" -gt 0 ]; then
  echo "PR changes a full-sweep path (${triggered[*]})" > "$RUNNER_TEMP/full_sweep.txt"
else
  : > "$RUNNER_TEMP/full_sweep.txt"
fi
