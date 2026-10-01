#!/usr/bin/env bash
# Provision the pinned Google style package, then run Vale over the files prek hands us.
#
# WHY THIS IS A SCRIPT AND NOT A `bash -c` ENTRY. An entry of
# `[ -d styles/Google ] || vale sync || exit 1; exec vale "$@"` fails, because prek
# splits a hook's file list across SEVERAL CONCURRENT invocations — ten of them for
# `--all-files` here. In a fresh worktree all ten start with styles/Google missing, so all
# ten run `vale sync` into the same directory at once and one of them dies with
# `unlinkat .../styles/Google: directory not empty` while a sibling is still unpacking into
# it. Every invocation reports `0 errors`; the hook fails on the sync alone.
# A second run passes, because by then the package is there — which is what makes this an
# only-in-a-fresh-worktree failure.
#
# THE LOCK IS ON styles/, NOT styles/Google. The critical section removes styles/Google, so
# a lock held on that directory would leave every waiter holding a lock on a deleted inode
# and no mutual exclusion at all. Locking the parent is the reason this works. It is opened
# read-only on fd 9 — flock(2) locks the open file description, so no lock file is created
# and nothing untracked lands in the checkout (an untracked file parks the GitOps deployer).
#
# THE STAMP, NOT THE DIRECTORY, IS THE GUARD. `[ -d styles/Google ]` also passes for a
# half-unpacked tree left by an interrupted sync, and Vale lints happily with a partial rule
# set: fewer rules, no error, a green hook checking less than it claims. The stamp is written
# only after `vale sync` exits 0, and it holds `.vale.ini`'s pinned `Packages =` line, so a
# Renovate bump of the package URL re-syncs every worktree instead of leaving them on the old
# release. It lives inside styles/Google/, which .gitignore already excludes and which the
# `rm -rf` below clears.
#
# The fast path skips the lock entirely: a provisioned worktree pays one `cat` per
# invocation, which is what the original `-d` guard was there to buy (`vale sync`
# re-downloads unconditionally, 0.3-0.5s and a network round-trip).
#
# With no filenames the script provisions and exits 0 — that is how CI installs the package
# before it runs the hooks, so CI and the hook share one provisioning path.
#
# Run: scripts/validate/vale.sh [file ...]
set -euo pipefail

# `--help` answers from any environment, ahead of the sync below, which needs the `vale`
# binary that a CI pytest shard does not install. The awk prints this
# file's leading comment block, which is the usage, with the `#` markers stripped.
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
styles="$repo/styles"
stamp="$styles/Google/.synced"

want="$(grep -m1 -E '^Packages[[:space:]]*=' "$repo/.vale.ini" || true)"
if [ -z "$want" ]; then
  echo "vale.sh: no pinned 'Packages =' line in $repo/.vale.ini" >&2
  exit 1
fi

if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
  mkdir -p "$styles"
  exec 9<"$styles"
  # -w rather than an indefinite block: this runs on every doc-touching commit, and a wedged
  # commit with no output is worse than a loud failure. The sync it waits on takes under a second.
  if ! flock -w 60 9; then
    echo "vale.sh: timed out waiting for the styles/ sync lock" >&2
    exit 1
  fi
  # Re-read under the lock: the waiters were all racing on the same missing package, and by
  # the time they get the lock the winner has fetched it.
  if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
    rm -rf "$styles/Google"
    (cd "$repo" && vale sync)
    printf '%s\n' "$want" >"$stamp"
  fi
  exec 9<&-
fi

if [ "$#" -eq 0 ]; then
  exit 0
fi
exec vale "$@"
