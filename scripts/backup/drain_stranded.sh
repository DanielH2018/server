#!/usr/bin/env bash
# Drain stranded Longhorn backup prefixes from B2.
#
# A convenience wrapper over ansible/drain_backup_prefix.yml, which exists because the full
# invocation is long enough to wrap in a terminal — and a wrapped command is not one command,
# it is several, which is how the first two attempts at this turned into "command not found".
#
#   ./scripts/backup/drain_stranded.sh          # dry run, deletes nothing
#   ./scripts/backup/drain_stranded.sh apply    # actually delete
#
# The allow-list is the file below. Read it before running apply; every name in it is deleted
# from the backup store if it is genuinely stranded, and the playbook refuses any whose Longhorn
# volume still exists.
#
# The list is printed with its age before every run, and cleared after a successful apply. Both
# exist because the file outlives the run that used it: after the 2026-08-20 drain it sat in
# /var/tmp for a day still naming a prefix that had already been deleted, which is the input a
# later `apply` would have acted on without anyone re-deriving it.
set -euo pipefail

LIST="${DRAIN_LIST:-/var/tmp/longhorn-stranded-prefixes.txt}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ ! -s "$LIST" ]; then
  echo "no volume list at $LIST — set DRAIN_LIST to point at one" >&2
  exit 2
fi

APPLY=false
if [ "${1:-}" = "apply" ]; then
  APPLY=true
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [apply]" >&2
  exit 2
fi

cd "$REPO"
echo "draining from $LIST (apply=$APPLY), written $(date -r "$LIST" '+%Y-%m-%d %H:%M')"
echo "--- allow-list ---"
cat "$LIST"
echo "------------------"

# Not exec'd: the apply has to be able to clear the list afterwards.
uv run ansible-playbook ansible/drain_backup_prefix.yml \
  -e "drain_apply=$APPLY" \
  -e "drain_volumes_file=$LIST"

if [ "$APPLY" = true ]; then
  rm -f "$LIST"
  echo "cleared $LIST — re-derive it before the next apply"
fi
