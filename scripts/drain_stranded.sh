#!/usr/bin/env bash
# Drain stranded Longhorn backup prefixes from B2.
#
# A convenience wrapper over ansible/drain_backup_prefix.yml, which exists because the full
# invocation is long enough to wrap in a terminal — and a wrapped command is not one command,
# it is several, which is how the first two attempts at this turned into "command not found".
#
#   ./scripts/drain_stranded.sh          # dry run, deletes nothing
#   ./scripts/drain_stranded.sh apply    # actually delete
#
# The allow-list is the file below. Read it before running apply; every name in it is deleted
# from the backup store if it is genuinely stranded, and the playbook refuses any whose Longhorn
# volume still exists.
set -euo pipefail

LIST="${DRAIN_LIST:-/var/tmp/longhorn-stranded-prefixes.txt}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
echo "draining from $LIST (apply=$APPLY)"
exec uv run ansible-playbook ansible/drain_backup_prefix.yml \
  -e "drain_apply=$APPLY" \
  -e "drain_volumes_file=$LIST"
